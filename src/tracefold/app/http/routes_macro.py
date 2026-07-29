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
    MACRO_MODULE_IDS,
    MACRO_MODULE_LABELS,
    MacroModuleId,
    MacroReason,
    macro_reason,
    project_alternative_presentation,
    project_asset_presentation,
    project_claim_presentation,
    project_live_delta_for_read,
    project_mainline_presentation,
    project_module_annotations,
    project_module_reader_narrative,
    project_outcome_replay_for_read,
    project_publication_appendix,
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
    with runtime.repositories() as repos:
        module_rows = {row["module_id"]: row for row in repos.macro.all_modules_current()}
        requested_state = repos.macro_thesis.state(session_date)
        fallback_row = (
            repos.macro_thesis.prior_publication(session_date) if _thesis_payload(requested_state) is None else None
        )
        displayed_state = requested_state if _thesis_payload(requested_state) is not None else fallback_row
        live_delta, outcome_replay = _follow_up_payloads(repos, displayed_state)

    thesis = _thesis_payload(displayed_state)
    displayed_session_date = date.fromisoformat(str(thesis["session_date"])) if thesis is not None else None
    thesis_reason = _thesis_reason(
        requested_state,
        session_date=session_date,
        cutoff_ms=thesis_cutoff_ms(session_date),
        read_at_ms=read_at_ms,
    )
    fallback = _fallback_context(
        requested_state=requested_state,
        fallback_row=fallback_row,
        requested_session=session_date,
    )
    role_by_module = {
        str(item["module_id"]): str(item["role"])
        for item in (thesis or {}).get("module_assessments", ())
        if isinstance(item, dict) and item.get("module_id") and item.get("role")
    }
    thesis_context_by_module = {
        module_id: _module_thesis_context(
            module_id,
            thesis=thesis,
            displayed_session_date=displayed_session_date,
            requested_session_date=session_date,
            requested_reason=thesis_reason,
        )
        for module_id in MACRO_MODULE_IDS
    }
    modules = [
        _module_summary(
            module_id,
            row=module_rows.get(module_id),
            role=role_by_module.get(module_id),
            thesis_context=thesis_context_by_module[module_id],
            backfill_worker_enabled=bool(runtime.settings.workers.macro_backfill.enabled),
        )
        for module_id in MACRO_MODULE_IDS
    ]
    payload = {
        "schema_version": "macro_overview_v7",
        "read_at_ms": read_at_ms,
        "transport": {
            "state": "current",
            "last_successful_read_at_ms": read_at_ms,
            "reason": None,
        },
        "session_date": session_date,
        "displayed_session_date": displayed_session_date,
        "cutoff_ms": (
            int(requested_state["cutoff_ms"]) if requested_state is not None else thesis_cutoff_ms(session_date)
        ),
        "latest_fact_at_ms": max(
            (int(module["latest_fact_at_ms"]) for module in modules if module["latest_fact_at_ms"] is not None),
            default=0,
        ),
        "thesis_state": _overview_thesis_state(requested_state),
        "thesis_reason": thesis_reason,
        "thesis": thesis,
        "run": _run_payload(requested_state, reason=thesis_reason),
        "fallback": fallback,
        "mainline_presentation": (
            mainline.model_dump(mode="json")
            if (mainline := project_mainline_presentation(thesis)) is not None
            else None
        ),
        "asset_presentation": [item.model_dump(mode="json") for item in project_asset_presentation(thesis)],
        "claim_presentation": [item.model_dump(mode="json") for item in project_claim_presentation(thesis)],
        "alternative_presentation": (
            alternative.model_dump(mode="json")
            if (alternative := project_alternative_presentation(thesis)) is not None
            else None
        ),
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
    read_at_ms = _now_ms()
    session_date = resolve_thesis_session(now_ms=read_at_ms)
    with runtime.repositories() as repos:
        row = repos.macro.module_current(module_id)
        requested_state = repos.macro_thesis.state(session_date)
        fallback_row = (
            repos.macro_thesis.prior_publication(session_date) if _thesis_payload(requested_state) is None else None
        )
    thesis = _thesis_payload(requested_state) or _thesis_payload(fallback_row)
    displayed_session_date = date.fromisoformat(str(thesis["session_date"])) if thesis is not None else None
    context = _module_thesis_context(
        module_id,
        thesis=thesis,
        displayed_session_date=displayed_session_date,
        requested_session_date=session_date,
        requested_reason=_thesis_reason(
            requested_state,
            session_date=session_date,
            cutoff_ms=thesis_cutoff_ms(session_date),
            read_at_ms=read_at_ms,
        ),
    )
    payload, reason = _module_payload(module_id, row)
    if payload is None:
        return _module_unavailable(module_id, reason=reason, thesis_context=context)
    payload["availability"] = "available"
    payload["thesis_context"] = context
    _apply_backfill_worker_state(
        payload,
        worker_enabled=bool(runtime.settings.workers.macro_backfill.enabled),
    )
    payload["reason"] = _available_module_reason(payload, thesis_context=context)
    return payload


@router.get(
    "/macro/rates-fed",
    response_model=api_schemas.ApiEnvelope[api_schemas.MacroRatesFedReadData | api_schemas.MacroModuleUnavailableData],
)
def macro_rates_fed(request: Request) -> JSONResponse:
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.MacroRatesFedReadData | api_schemas.MacroModuleUnavailableData],
        {"ok": True, "data": _read_module(request, "rates_fed")},
    )


@router.get(
    "/macro/economy-inflation",
    response_model=api_schemas.ApiEnvelope[
        api_schemas.MacroEconomyInflationReadData | api_schemas.MacroModuleUnavailableData
    ],
)
def macro_economy_inflation(request: Request) -> JSONResponse:
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.MacroEconomyInflationReadData | api_schemas.MacroModuleUnavailableData],
        {"ok": True, "data": _read_module(request, "economy_inflation")},
    )


@router.get(
    "/macro/liquidity-funding",
    response_model=api_schemas.ApiEnvelope[
        api_schemas.MacroLiquidityFundingReadData | api_schemas.MacroModuleUnavailableData
    ],
)
def macro_liquidity_funding(request: Request) -> JSONResponse:
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.MacroLiquidityFundingReadData | api_schemas.MacroModuleUnavailableData],
        {"ok": True, "data": _read_module(request, "liquidity_funding")},
    )


@router.get(
    "/macro/credit",
    response_model=api_schemas.ApiEnvelope[api_schemas.MacroCreditReadData | api_schemas.MacroModuleUnavailableData],
)
def macro_credit(request: Request) -> JSONResponse:
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.MacroCreditReadData | api_schemas.MacroModuleUnavailableData],
        {"ok": True, "data": _read_module(request, "credit")},
    )


@router.get(
    "/macro/volatility",
    response_model=api_schemas.ApiEnvelope[
        api_schemas.MacroVolatilityReadData | api_schemas.MacroModuleUnavailableData
    ],
)
def macro_volatility(request: Request) -> JSONResponse:
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.MacroVolatilityReadData | api_schemas.MacroModuleUnavailableData],
        {"ok": True, "data": _read_module(request, "volatility")},
    )


@router.get(
    "/macro/cross-asset",
    response_model=api_schemas.ApiEnvelope[
        api_schemas.MacroCrossAssetReadData | api_schemas.MacroModuleUnavailableData
    ],
)
def macro_cross_asset(request: Request) -> JSONResponse:
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.MacroCrossAssetReadData | api_schemas.MacroModuleUnavailableData],
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
    read_at_ms = _now_ms()
    current_session = resolve_thesis_session(now_ms=read_at_ms)
    target_session = session_date or current_session
    with runtime.repositories() as repos:
        requested_state = repos.macro_thesis.state(target_session)
        fallback_row = (
            repos.macro_thesis.prior_publication(target_session)
            if target_session == current_session and _thesis_payload(requested_state) is None
            else None
        )
        displayed_state = requested_state if _thesis_payload(requested_state) is not None else fallback_row
        thesis = _thesis_payload(displayed_state)
        current_publication_displayed = (
            target_session == current_session
            and _thesis_payload(requested_state) is not None
            and thesis is not None
            and str(thesis["session_date"]) == current_session.isoformat()
        )
        live_delta, outcome_replay = (
            _follow_up_payloads(repos, displayed_state) if current_publication_displayed else (None, None)
        )
        appendix = _publication_appendix_payload(repos, thesis)
        history = [_history_payload(item) for item in repos.macro_thesis.publications(limit=30)]
    displayed_session_date = date.fromisoformat(str(thesis["session_date"])) if thesis is not None else None
    reason = _thesis_reason(
        requested_state,
        session_date=target_session,
        cutoff_ms=thesis_cutoff_ms(target_session),
        read_at_ms=read_at_ms,
    )
    payload = {
        "schema_version": "macro_thesis_detail_v3",
        "state": _detail_state(
            requested_state,
            requested_session=target_session,
            current_session=current_session,
        ),
        "requested_session_date": target_session,
        "current_session_date": current_session,
        "displayed_session_date": displayed_session_date,
        "reason": reason,
        "thesis": thesis,
        "fallback": _fallback_context(
            requested_state=requested_state,
            fallback_row=fallback_row,
            requested_session=target_session,
        ),
        "mainline_presentation": (
            mainline.model_dump(mode="json")
            if (mainline := project_mainline_presentation(thesis)) is not None
            else None
        ),
        "asset_presentation": [item.model_dump(mode="json") for item in project_asset_presentation(thesis)],
        "claim_presentation": [item.model_dump(mode="json") for item in project_claim_presentation(thesis)],
        "alternative_presentation": (
            alternative.model_dump(mode="json")
            if (alternative := project_alternative_presentation(thesis)) is not None
            else None
        ),
        "live_delta": live_delta,
        "outcome_replay": outcome_replay,
        "appendix": appendix,
        "run": _run_payload(requested_state, reason=reason),
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
        message=f"{MACRO_MODULE_LABELS[module_id]}的持久化 schema 与当前 hard-cut 合同不一致。",
        impact="blocked",
        retryable=True,
        recovery="operator_action",
        next_action=f"使用 {expected_schema} 重新投影该模块。",
    )


def _module_summary(
    module_id: MacroModuleId,
    *,
    row: dict[str, Any] | None,
    role: str | None,
    thesis_context: dict[str, Any],
    backfill_worker_enabled: bool,
) -> dict[str, Any]:
    payload, reason = _module_payload(module_id, row)
    if payload is None:
        return {
            "module_id": module_id,
            "label": MACRO_MODULE_LABELS[module_id],
            "availability": "unavailable",
            "reason": reason,
            "role": role,
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
            "thesis_context": thesis_context,
        }
    _apply_backfill_worker_state(payload, worker_enabled=backfill_worker_enabled)
    status = dict(payload["status"])
    coverage = dict(status["coverage"])
    current_health = dict(status["current_health"])
    history_depth = dict(status["history_depth"])
    available_reason = _available_module_reason(payload, thesis_context=thesis_context)
    return {
        "module_id": payload["module_id"],
        "label": payload["label"],
        "availability": "available",
        "reason": available_reason,
        "role": role,
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
        "href": _MODULE_HREFS[str(payload["module_id"])],
        "thesis_context": thesis_context,
    }


def _available_module_reason(
    payload: dict[str, Any],
    *,
    thesis_context: dict[str, Any],
) -> dict[str, object] | None:
    status = dict(payload["status"])
    coverage = dict(status["coverage"])
    current_health = dict(status["current_health"])
    history_depth = dict(status["history_depth"])
    backfill = dict(status["backfill_execution"])
    evidence = dict(payload["evidence"])
    dataset_states = [item for item in evidence["dataset_states"] if isinstance(item, dict)]

    axes: list[str] = []
    candidates: list[MacroReason] = []
    affected_dataset_ids: set[str] = set()

    def add_reason(value: object) -> None:
        if not isinstance(value, dict):
            return
        reason = MacroReason.model_validate(value)
        if reason.impact == "none":
            return
        candidates.append(reason)
        affected_dataset_ids.update(reason.affected_dataset_ids)

    if coverage["state"] == "partial":
        axes.append("能力覆盖")
        for capability in coverage["capabilities"]:
            if not isinstance(capability, dict) or capability.get("state") == "available":
                continue
            affected_dataset_ids.update(str(item) for item in capability.get("dataset_ids", ()))
            add_reason(capability.get("reason"))

    if current_health["state"] != "current":
        axes.append("当前事实")
        for state in dataset_states:
            if state.get("current_health") == "current":
                continue
            if state.get("dataset_id"):
                affected_dataset_ids.add(str(state["dataset_id"]))
            add_reason(state.get("current_reason"))

    if history_depth["state"] in {"partial", "insufficient"}:
        axes.append("历史深度")
        for state in dataset_states:
            if state.get("history_depth") not in {"partial", "insufficient"}:
                continue
            if state.get("dataset_id"):
                affected_dataset_ids.add(str(state["dataset_id"]))
            add_reason(state.get("history_reason"))

    if backfill["state"] not in {"not_required", "complete"}:
        axes.append("历史回填")
        add_reason(backfill.get("reason"))

    if not axes:
        return None

    if not affected_dataset_ids:
        affected_dataset_ids.update(str(state["dataset_id"]) for state in dataset_states if state.get("dataset_id"))
    affected_claim_ids = tuple(dict.fromkeys(str(item) for item in thesis_context.get("claim_ids", ()) if item))
    next_actions = tuple(dict.fromkeys(reason.next_action for reason in candidates if reason.next_action is not None))
    next_checks = tuple(reason.next_check_at_ms for reason in candidates if reason.next_check_at_ms is not None)
    recoveries = {reason.recovery for reason in candidates}
    recovery = (
        "operator_action"
        if "operator_action" in recoveries
        else "automatic"
        if "automatic" in recoveries
        else "next_session"
        if "next_session" in recoveries
        else "none"
    )
    current_state = str(current_health["state"])
    code = (
        "macro_module_current_unavailable"
        if current_state == "unavailable"
        else "macro_module_current_degraded"
        if current_state == "degraded"
        else "macro_module_coverage_partial"
        if coverage["state"] == "partial"
        else "macro_module_history_incomplete"
    )
    next_action = "；".join(next_actions[:3]) or "检查受影响 Dataset 状态后重新投影该模块。"
    return macro_reason(
        code=code,
        message=f"{payload['label']}仍可读取，但{'、'.join(dict.fromkeys(axes))}存在缺口。",
        impact="blocked" if current_state == "unavailable" else "limited",
        affected_dataset_ids=tuple(sorted(affected_dataset_ids)),
        affected_claim_ids=affected_claim_ids,
        retryable=any(reason.retryable for reason in candidates),
        recovery=recovery,
        next_action=next_action[:2_000],
        next_check_at_ms=min(next_checks, default=None),
    )


def _module_unavailable(
    module_id: MacroModuleId,
    *,
    reason: dict[str, object] | None,
    thesis_context: dict[str, Any],
) -> dict[str, Any]:
    if reason is None:
        raise ValueError("macro_module_unavailable_reason_required")
    return {
        "schema_version": "macro_module_unavailable_v1",
        "module_id": module_id,
        "label": MACRO_MODULE_LABELS[module_id],
        "availability": "unavailable",
        "reason": reason,
        "href": _MODULE_HREFS[module_id],
        "thesis_context": thesis_context,
    }


def _apply_backfill_worker_state(payload: dict[str, Any], *, worker_enabled: bool) -> None:
    status = payload.get("status")
    if not isinstance(status, dict):
        return
    execution = status.get("backfill_execution")
    if not isinstance(execution, dict):
        return
    execution["worker_enabled"] = worker_enabled
    state = str(execution.get("state") or "")
    if not worker_enabled and state in {"queued", "running", "retry_wait"}:
        affected = tuple(
            sorted(
                str(item["dataset_id"])
                for item in dict(payload.get("evidence") or {}).get("dataset_states", ())
                if isinstance(item, dict) and item.get("history_depth") in {"partial", "insufficient"}
            )
        )
        execution.update(
            {
                "state": "paused",
                "next_check_at_ms": None,
                "reason": macro_reason(
                    code="history_backfill_worker_disabled",
                    message="历史回填目标仍未完成，但 macro_backfill worker 已停用。",
                    impact="limited",
                    affected_dataset_ids=affected,
                    retryable=True,
                    recovery="operator_action",
                    next_action="启用 macro_backfill worker 或由操作员执行明确的回填任务。",
                ),
            }
        )
    elif worker_enabled and state == "paused":
        execution.update(
            {
                "state": "queued",
                "reason": macro_reason(
                    code="history_backfill_queued",
                    message="历史回填 worker 已启用，未完成目标等待领取。",
                    impact="limited",
                    retryable=True,
                    recovery="automatic",
                    next_action="等待 macro_backfill worker 领取目标。",
                    next_check_at_ms=execution.get("next_check_at_ms"),
                ),
            }
        )


def _module_thesis_context(
    module_id: MacroModuleId,
    *,
    thesis: dict[str, Any] | None,
    displayed_session_date: date | None,
    requested_session_date: date,
    requested_reason: dict[str, object] | None,
) -> dict[str, Any]:
    if thesis is None:
        reason_code = str((requested_reason or {}).get("code") or "")
        state = (
            "generating"
            if reason_code
            in {
                "macro_thesis_pending",
                "macro_thesis_running",
                "macro_thesis_retryable",
                "macro_thesis_provider_transient",
            }
            else "not_published"
            if reason_code == "macro_thesis_not_published"
            else "failed"
            if reason_code.startswith("macro_thesis_") and reason_code not in {"macro_thesis_run_missing"}
            else "missing"
        )
        return {
            "state": state,
            "session_date": None,
            "cutoff_ms": None,
            "role": None,
            "reader_narrative": None,
            "claim_ids": [],
            "supporting_evidence_refs": [],
            "conflicting_evidence_refs": [],
            "annotations": [],
            "reason": requested_reason,
        }
    assessment = next(
        (
            item
            for item in thesis.get("module_assessments", ())
            if isinstance(item, dict) and item.get("module_id") == module_id
        ),
        None,
    )
    historical = displayed_session_date != requested_session_date
    return {
        "state": "historical" if historical else "current",
        "session_date": displayed_session_date,
        "cutoff_ms": int(thesis["cutoff_ms"]),
        "role": assessment.get("role") if assessment is not None else None,
        "reader_narrative": (
            narrative.model_dump(mode="json")
            if (narrative := project_module_reader_narrative(thesis, module_id=module_id)) is not None
            else None
        ),
        "claim_ids": list(assessment.get("claim_ids") or ()) if assessment is not None else [],
        "supporting_evidence_refs": (
            list(assessment.get("supporting_evidence_refs") or ()) if assessment is not None else []
        ),
        "conflicting_evidence_refs": (
            list(assessment.get("conflicting_evidence_refs") or ()) if assessment is not None else []
        ),
        "annotations": [
            item.model_dump(mode="json") for item in project_module_annotations(thesis, module_id=module_id)
        ],
        "reason": (
            macro_reason(
                code="module_context_uses_prior_publication",
                message="该模块角色来自最近一份历史 Thesis，不代表 requested session 已发布。",
                impact="limited",
                affected_claim_ids=tuple(assessment.get("claim_ids") or ()) if assessment is not None else (),
                retryable=True,
                recovery="next_session",
                next_action="继续展示历史角色，同时等待 requested session 形成新 publication。",
            )
            if historical
            else None
        ),
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
        (str(module["history_depth_state"]) if module["history_depth_state"] is not None else "insufficient")
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


def _thesis_reason(
    row: dict[str, Any] | None,
    *,
    session_date: date,
    cutoff_ms: int,
    read_at_ms: int,
) -> dict[str, object] | None:
    if row is not None and isinstance(row.get("thesis_json"), dict):
        return None
    if row is None:
        before_cutoff = read_at_ms < cutoff_ms
        return macro_reason(
            code="macro_thesis_run_missing",
            message=(
                "requested session 尚未到 08:50 ET publication cutoff。"
                if before_cutoff
                else "requested session 没有持久化 Thesis run。"
            ),
            impact="blocked",
            retryable=True,
            recovery="automatic" if before_cutoff else "operator_action",
            next_action=(
                "等待 08:50 ET 后由 macro_thesis worker 创建运行。"
                if before_cutoff
                else "检查 macro_thesis worker 配置并显式触发或恢复该 session。"
            ),
            next_check_at_ms=cutoff_ms if before_cutoff else None,
        )
    status = str(row.get("status") or "missing")
    error_code = str(row.get("last_error_code") or "")
    if status in _GENERATING_RUN_STATES:
        transient_provider_failure = status == "retryable"
        return macro_reason(
            code=("macro_thesis_provider_transient" if transient_provider_failure else f"macro_thesis_{status}"),
            message={
                "pending": "Thesis run 已创建，尚未被 worker 领取。",
                "running": "Thesis Agent 或独立 Reviewer 正在运行。",
                "retryable": "上一次模型 provider 调用发生暂时性故障，运行正在等待自动重试。",
            }[status],
            impact="limited",
            retryable=True,
            recovery="automatic",
            next_action="等待持久化运行状态推进；读取页面不会启动或恢复 Agent。",
            next_check_at_ms=(
                int(row["leased_until_ms"])
                if status == "running" and row.get("leased_until_ms") is not None
                else int(row["due_at_ms"])
                if row.get("due_at_ms") is not None
                else None
            ),
        )
    stable_code = error_code or f"macro_thesis_{status}"
    transient_provider_exhausted = status == "failed" and _is_transient_thesis_error_code(stable_code)
    if transient_provider_exhausted:
        return macro_reason(
            code="macro_thesis_provider_retry_exhausted",
            message="模型 provider 的暂时性故障已耗尽本 session 固定重试次数，未形成 publication。",
            impact="blocked",
            retryable=False,
            recovery="next_session",
            next_action="保留失败诊断并检查 provider 健康；等待下一交易日使用新 Evidence Pack 创建新运行。",
        )
    message = {
        "macro_thesis_agent_step_limit": "研究 Agent 达到固定步骤上限，本 session 未发布。",
        "macro_thesis_reviewer_block": "一次定向修订后独立 Reviewer 仍未通过 publication gate，本 session 未发布。",
        "macro_thesis_configuration_error": "Thesis model/provider 配置不兼容，本 session 未启动可重试运行。",
    }.get(
        stable_code,
        "Thesis run 已结束但没有可发布 publication。"
        if status in (_FAILED_RUN_STATES | _NOT_PUBLISHED_RUN_STATES)
        else "requested session 尚无可发布 Thesis。",
    )
    configuration_error = (
        status == "config_error" or "configuration" in stable_code or "unsupported_model" in stable_code
    )
    reviewer_block = stable_code == "macro_thesis_reviewer_block"
    step_limit = stable_code == "macro_thesis_agent_step_limit"
    not_published = status in _NOT_PUBLISHED_RUN_STATES
    return macro_reason(
        code=stable_code,
        message=message,
        impact="blocked",
        retryable=False,
        recovery=(
            "operator_action"
            if configuration_error or status in _FAILED_RUN_STATES
            else "next_session"
            if reviewer_block or step_limit or not_published
            else "operator_action"
        ),
        next_action=(
            "修复 model/provider 配置后显式重试该 session。"
            if configuration_error
            else "保留 Reviewer 阻断及定向修订记录，等待下一交易日的新冻结 Evidence Pack。"
            if reviewer_block
            else "保留 step-limit 失败诊断，等待下一交易日的新冻结 Evidence Pack。"
            if step_limit
            else "检查持久化 run/review 诊断；不得用旧 publication 冒充本 session。"
            if status in _FAILED_RUN_STATES
            else "保留未发布结果，等待下一交易日的新冻结 Evidence Pack。"
            if not_published
            else "检查 macro_thesis worker 与 session 调度。"
        ),
    )


def _is_transient_thesis_error_code(error_code: str) -> bool:
    normalized = error_code.lower().replace("_", "")
    return any(
        token in normalized
        for token in (
            "timeout",
            "ratelimit",
            "connection",
            "serviceunavailable",
            "temporar",
        )
    )


def _fallback_context(
    *,
    requested_state: dict[str, Any] | None,
    fallback_row: dict[str, Any] | None,
    requested_session: date,
) -> dict[str, Any]:
    if fallback_row is not None and isinstance(fallback_row.get("thesis_json"), dict):
        thesis = dict(fallback_row["thesis_json"])
        return {
            "state": "available",
            "reason": macro_reason(
                code="prior_publication_displayed",
                message="requested session 尚未发布；页面继续展示最近一份有效历史 Thesis。",
                impact="limited",
                retryable=True,
                recovery="next_session",
                next_action="保留历史标记并等待 requested session 产生新的 publication。",
            ),
            "publication_id": thesis["publication_id"],
            "session_date": thesis["session_date"],
            "cutoff_ms": int(thesis["cutoff_ms"]),
        }
    current_available = _thesis_payload(requested_state) is not None
    return {
        "state": "none",
        "reason": macro_reason(
            code="fallback_not_required" if current_available else "prior_publication_missing",
            message=(
                "requested session 已有 publication，无需 fallback。"
                if current_available
                else f"{requested_session.isoformat()} 之前没有可展示的历史 publication。"
            ),
            impact="none" if current_available else "blocked",
            retryable=False,
            recovery="none" if current_available else "next_session",
            next_action=None if current_available else "等待首份通过 publication gate 的 Thesis。",
        ),
        "publication_id": None,
        "session_date": None,
        "cutoff_ms": None,
    }


def _thesis_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None or not isinstance(row.get("thesis_json"), dict):
        return None
    return dict(row["thesis_json"])


def _follow_up_payloads(
    repos: Any,
    state: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    thesis = _thesis_payload(state)
    if state is None or not state.get("publication_id") or thesis is None:
        return None, None
    publication_id = str(state["publication_id"])
    live_row = repos.macro_thesis.latest_live_delta(publication_id)
    outcome_row = repos.macro_thesis.latest_outcome_replay(publication_id)
    live_delta = (
        project_live_delta_for_read(
            payload=dict(live_row["payload_json"]),
            publication=thesis,
        ).model_dump(mode="json")
        if live_row is not None and isinstance(live_row.get("payload_json"), dict)
        else None
    )
    outcome_replay = (
        project_outcome_replay_for_read(dict(outcome_row["payload_json"])).model_dump(mode="json")
        if outcome_row is not None and isinstance(outcome_row.get("payload_json"), dict)
        else None
    )
    return live_delta, outcome_replay


def _publication_appendix_payload(
    repos: Any,
    thesis: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if thesis is None:
        return None
    evidence_pack_id = str(thesis["evidence_pack_id"])
    row = repos.macro_thesis.evidence_pack(evidence_pack_id)
    if row is None or not isinstance(row.get("payload_json"), dict):
        raise ValueError("macro_research_publication_evidence_pack_missing")
    return project_publication_appendix(
        publication=thesis,
        evidence_pack=dict(row["payload_json"]),
    ).model_dump(mode="json")


def _run_payload(
    row: dict[str, Any] | None,
    *,
    reason: dict[str, object] | None,
) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "session_date": row["session_date"],
        "status": str(row["status"]),
        "evidence_pack_id": str(row["evidence_pack_id"]),
        "attempt_count": int(row["attempt_count"]),
        "max_attempts": int(row["max_attempts"]),
        "error_code": row.get("last_error_code"),
        "reason": reason,
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
