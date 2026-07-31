from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from tracefold.macro.assets import MACRO_ASSET_DATASET_IDS
from tracefold.macro.calculations import (
    calculate_curve_contract,
    calculate_funding_cost_comparisons,
    calculate_market_comparison,
    calculate_market_statistics,
    calculate_series_statistics,
    natural_change_calculation,
)
from tracefold.macro.coverage import coverage_for_module
from tracefold.macro.domain import MACRO_MODULE_LABELS, DatasetSpec, MacroModuleId
from tracefold.macro.fed_roles import effective_roster_rows, match_effective_role
from tracefold.macro.market_calendar import market_clock
from tracefold.macro.reasons import macro_reason
from tracefold.macro.registry import DATASET_REGISTRY, datasets_for_module

_SCHEMA_VERSIONS: dict[MacroModuleId, str] = {
    "rates_fed": "macro_rates_fed_v6",
    "economy_inflation": "macro_economy_inflation_v5",
    "liquidity_funding": "macro_liquidity_funding_v5",
    "credit": "macro_credit_v7",
    "volatility": "macro_volatility_v7",
    "cross_asset": "macro_cross_asset_v7",
}
_ETF_DAILY_IDS = (
    "nasdaq.spy.daily",
    "nasdaq.qqq.daily",
    "nasdaq.iwm.daily",
    "nasdaq.tlt.daily",
    "nasdaq.ief.daily",
    "nasdaq.lqd.daily",
    "nasdaq.hyg.daily",
    "nasdaq.dxy.daily",
    "nasdaq.gld.daily",
    "nasdaq.uso.daily",
)
_ETF_INTRADAY_IDS = tuple(
    dataset_id.replace("nasdaq.", "yfinance.").replace(".daily", ".intraday") for dataset_id in _ETF_DAILY_IDS
)
_ETF_DATASETS = tuple(zip(_ETF_DAILY_IDS, _ETF_INTRADAY_IDS, strict=True))
_CROSS_ASSET_NORMALIZED_GROUPS = (
    ("equity", "权益", _ETF_DAILY_IDS[:3]),
    ("duration_credit", "久期与信用", _ETF_DAILY_IDS[3:7]),
    ("dollar_commodities", "美元与商品", _ETF_DAILY_IDS[7:]),
)
_FUTURES_DAILY_IDS = (
    "yfinance.es_future.daily",
    "yfinance.nq_future.daily",
    "yfinance.rty_future.daily",
    "yfinance.zb_future.daily",
    "yfinance.zn_future.daily",
    "yfinance.dx_future.daily",
    "yfinance.gc_future.daily",
    "yfinance.cl_future.daily",
    "yfinance.hg_future.daily",
)
_FUTURES_INTRADAY_IDS = tuple(dataset_id.replace(".daily", ".intraday") for dataset_id in _FUTURES_DAILY_IDS)
_FUTURES_DATASETS = tuple(zip(_FUTURES_DAILY_IDS, _FUTURES_INTRADAY_IDS, strict=True))
_HEALTH_GROUP_LABELS = {
    "etf_intraday": "ETF盘中行情",
    "etf_daily": "ETF五年日线",
    "futures_intraday": "连续期货盘中代理",
    "futures_daily": "连续期货五年日线",
    "continuous_intraday": "连续市场盘中行情",
}
_NEW_YORK = ZoneInfo("America/New_York")
_DAY_MS = 86_400_000


def build_typed_module_payload(
    *,
    module_id: MacroModuleId,
    now_ms: int,
    series_rows: list[dict[str, Any]],
    market_rows: list[dict[str, Any]],
    position_rows: list[dict[str, Any]],
    settlement_rows: list[dict[str, Any]],
    release_rows: list[dict[str, Any]],
    document_rows: list[dict[str, Any]],
    target_states: list[dict[str, Any]],
    role_rows: list[dict[str, Any]] | None = None,
    analysis_rows: list[dict[str, Any]] | None = None,
    analysis_job_state: dict[str, int] | None = None,
) -> dict[str, Any]:
    role_rows = role_rows or []
    analysis_rows = analysis_rows or []
    settlement_rows = _current_settlement_revisions(settlement_rows)
    specs = datasets_for_module(module_id)
    dataset_ids = {spec.dataset_id for spec in specs}
    module_facts = [
        row
        for row in (
            *series_rows,
            *market_rows,
            *position_rows,
            *settlement_rows,
            *release_rows,
            *document_rows,
            *role_rows,
            *analysis_rows,
        )
        if str(row["dataset_id"]) in dataset_ids
    ]
    latest_fact_at_ms = max((_fact_clock_ms(row) for row in module_facts), default=0)
    dataset_states = _dataset_states(
        specs,
        target_states,
        module_facts,
        now_ms,
        analysis_job_state=analysis_job_state,
    )
    coverage = _coverage(module_id)
    current_health = _current_health(dataset_states)
    history_depth = _history_depth(dataset_states)
    backfill_execution = _backfill_execution(specs, target_states)
    payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSIONS[module_id],
        "module_id": module_id,
        "label": MACRO_MODULE_LABELS[module_id],
        "status": {
            "coverage": coverage,
            "current_health": current_health,
            "history_depth": history_depth,
            "backfill_execution": backfill_execution,
        },
        "latest_fact_at_ms": latest_fact_at_ms,
        "next_checkpoints": _next_checkpoints(dataset_states),
        "evidence": {
            "dataset_states": dataset_states,
            "latest_facts": _latest_fact_summaries(module_facts),
            "asset_changes": [],
            "reconciliation_receipts": _reconciliation_receipts(specs, module_facts),
        },
    }
    if module_id != "rates_fed":
        changes = _top_changes(specs, series_rows, market_rows, settlement_rows, release_rows)
        payload.update(
            {
                "summary": {
                    "headline": _summary_headline(changes),
                    "interpretation": None,
                    "top_changes": changes[:6],
                },
                "contradictions": _contradictions(module_id, changes),
                "falsifiers": [],
            }
        )
        payload["evidence"]["asset_changes"] = [
            change for change in changes if str(change["dataset_id"]) in MACRO_ASSET_DATASET_IDS
        ]
    builders = {
        "rates_fed": _rates_payload,
        "economy_inflation": _economy_payload,
        "liquidity_funding": _liquidity_payload,
        "credit": _credit_payload,
        "volatility": _volatility_payload,
        "cross_asset": _cross_asset_payload,
    }
    payload.update(
        builders[module_id](
            series_rows=series_rows,
            market_rows=market_rows,
            position_rows=position_rows,
            settlement_rows=settlement_rows,
            release_rows=release_rows,
            document_rows=document_rows,
            role_rows=role_rows,
            analysis_rows=analysis_rows,
        )
    )
    return payload


def schema_version_for_module(module_id: MacroModuleId) -> str:
    return _SCHEMA_VERSIONS[module_id]


def _coverage(module_id: MacroModuleId) -> dict[str, Any]:
    capabilities = []
    required_missing = False
    for capability in coverage_for_module(module_id):
        datasets = [DATASET_REGISTRY.get(dataset_id) for dataset_id in capability.dataset_ids]
        active = all(spec is not None for spec in datasets)
        state = "available" if active else "missing"
        if not active and capability.requirement == "required":
            required_missing = True
        capabilities.append(
            {
                "capability_id": capability.capability_id,
                "label": capability.label,
                "requirement": capability.requirement,
                "state": state,
                "dataset_ids": list(capability.dataset_ids),
                "reason": (
                    None
                    if active
                    else macro_reason(
                        code="dataset_not_registered",
                        message="该能力声明的 Dataset 尚未注册，不能作为当前模块证据。",
                        impact="blocked" if capability.requirement == "required" else "limited",
                        affected_dataset_ids=tuple(capability.dataset_ids),
                        retryable=False,
                        recovery="operator_action",
                        next_action="修复 Dataset Registry 后重新投影模块。",
                    )
                ),
            }
        )
    state = "partial" if required_missing else "complete"
    return {
        "state": state,
        "expected_capabilities": len(capabilities),
        "available_capabilities": sum(item["state"] == "available" for item in capabilities),
        "capabilities": capabilities,
    }


def _dataset_states(
    specs: tuple[DatasetSpec, ...],
    targets: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    now_ms: int,
    *,
    analysis_job_state: dict[str, int] | None,
) -> list[dict[str, Any]]:
    required_dataset_ids = {
        dataset_id
        for spec in specs
        for capability in coverage_for_module(spec.module_id)
        if capability.requirement == "required"
        for dataset_id in capability.dataset_ids
    }
    targets_by_dataset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in targets:
        targets_by_dataset[str(row["dataset_id"])].append(row)
    latest_by_dataset: dict[str, dict[str, Any]] = {}
    for row in facts:
        dataset_id = str(row["dataset_id"])
        if dataset_id not in latest_by_dataset or _fact_order(row) > _fact_order(latest_by_dataset[dataset_id]):
            latest_by_dataset[dataset_id] = row
    states = []
    for spec in specs:
        required_for_current = spec.dataset_id in required_dataset_ids and spec.source_role not in {
            "history",
            "intraday_proxy",
            "reconciliation_only",
        }
        required_for_history = _history_required(spec)
        dataset_targets = targets_by_dataset.get(spec.dataset_id, [])
        target = next(
            (row for row in dataset_targets if str(row.get("partition_key")) == "latest"),
            None,
        )
        active_backfills = [
            row
            for row in dataset_targets
            if str(row.get("clock_kind")) == "backfill"
            and str(row.get("status")) != "current"
            and isinstance(row.get("cursor_json"), dict)
        ]
        latest = latest_by_dataset.get(spec.dataset_id)
        source_state = "not_applicable" if spec.source_role == "derived" else _source_state(target)
        market = (
            market_clock(str(spec.metadata.get("market_calendar") or ""), now_ms=now_ms)
            if spec.clock_kind == "intraday_market"
            else None
        )
        if (
            spec.dataset_id == "federal_reserve.document.analysis"
            and analysis_job_state is not None
            and int(analysis_job_state.get("failed", 0)) > 0
        ):
            current_health = "degraded" if latest is not None else "unavailable"
            current_reason = "document_analysis_jobs_failed"
        elif (
            spec.dataset_id == "federal_reserve.document.analysis"
            and analysis_job_state is not None
            and int(analysis_job_state.get("open", 0)) > 0
            and latest is None
        ):
            current_health = "unavailable"
            current_reason = "document_analysis_initial_build_pending"
        elif spec.adapter_id.startswith("derived_") and latest is None:
            current_health = "unavailable"
            current_reason = "derived_fact_pending"
        elif spec.adapter_id.startswith("derived_"):
            if latest is None:
                raise RuntimeError("derived_macro_fact_missing")
            age_ms = _freshness_age_ms(spec, latest, now_ms)
            if age_ms > spec.freshness_seconds * 2_000:
                current_health, current_reason = "degraded", "derived_fact_past_freshness_budget"
            elif age_ms > spec.freshness_seconds * 1_000:
                current_health, current_reason = "degraded", "derived_fact_delayed"
            else:
                current_health, current_reason = "current", "within_freshness_budget"
        elif target is None:
            current_health = "unavailable"
            current_reason = "acquisition_target_missing"
            source_state = "failed"
        elif str(target.get("status") or "") in {
            "stale",
            "unavailable",
            "invalid",
            "failed",
        }:
            current_health = "degraded" if latest is not None else "unavailable"
            current_reason = "source_terminal_stale"
            source_state = "failed"
        elif latest is None:
            current_health = "unavailable"
            current_reason = "no_valid_fact"
        else:
            age_ms = _freshness_age_ms(
                spec,
                latest,
                now_ms,
                expected_at_ms=market.expected_at_ms if market is not None else None,
            )
            if age_ms > spec.freshness_seconds * 2_000:
                current_health, current_reason = "degraded", "expected_fact_missing"
            elif age_ms > spec.freshness_seconds * 1_000:
                current_health, current_reason = "degraded", "expected_fact_delayed"
            else:
                current_health = "current"
                current_reason = (
                    "last_expected_bar_present"
                    if market is not None and market.state in {"closed", "maintenance"}
                    else "within_freshness_budget"
                )
        history_state, history_reason = _dataset_history_depth(
            spec,
            dataset_targets,
            [row for row in facts if str(row["dataset_id"]) == spec.dataset_id],
            active_backfills=active_backfills,
        )
        current_next_check_at_ms = _minimum_timestamp(
            target.get("next_due_at_ms") if target is not None else None,
            market.next_open_ms if market is not None else None,
        )
        if current_reason == "source_terminal_stale":
            current_next_check_at_ms = None
        history_next_check_at_ms = min(
            (int(row["next_due_at_ms"]) for row in active_backfills if row.get("next_due_at_ms") is not None),
            default=None,
        )
        states.append(
            {
                "dataset_id": spec.dataset_id,
                "concept_id": spec.concept_id,
                "source_role": spec.source_role,
                "required_for_current": required_for_current,
                "required_for_history": required_for_history,
                "label": spec.label,
                "current_health": current_health,
                "history_depth": history_state,
                "market_state": market.state if market is not None else "not_applicable",
                "source_state": source_state,
                "current_reason": _dataset_current_reason(
                    code=current_reason,
                    dataset_id=spec.dataset_id,
                    current_health=current_health,
                    next_check_at_ms=current_next_check_at_ms,
                ),
                "history_reason": _dataset_history_reason(
                    code=history_reason,
                    dataset_id=spec.dataset_id,
                    history_depth=history_state,
                    required_for_history=required_for_history,
                    next_check_at_ms=history_next_check_at_ms,
                ),
                "critical": spec.critical,
                "trust_tier": spec.trust_tier,
                "source_url": spec.source_url,
                "latest_reference": _fact_reference(latest),
                "latest_received_at_ms": int(latest["received_at_ms"]) if latest else None,
                "last_market_at_ms": (
                    int(latest["observed_at_ms"])
                    if latest is not None and latest.get("observed_at_ms") is not None
                    else None
                ),
                "next_open_ms": market.next_open_ms if market is not None else None,
                "health_group": str(spec.metadata.get("health_group") or spec.clock_kind),
            }
        )
    return states


def _source_state(target: dict[str, Any] | None) -> str:
    if target is None:
        return "failed"
    status = str(target.get("status") or "")
    if status == "current":
        return "healthy"
    if status in {"stale", "unavailable", "invalid", "failed"}:
        return "failed"
    return "degraded"


_CURRENT_REASON_MESSAGES = {
    "document_analysis_jobs_failed": "部分政策文件分析任务失败，现有事实仍可读取但分析覆盖受限。",
    "document_analysis_initial_build_pending": "政策文件分析尚未形成首个有效事实。",
    "derived_fact_pending": "确定性派生事实尚未生成。",
    "derived_fact_past_freshness_budget": "派生事实已超过最大 freshness 预算。",
    "derived_fact_delayed": "派生事实已超过预期 freshness 预算。",
    "within_freshness_budget": "当前事实位于 Dataset freshness 预算内。",
    "acquisition_target_missing": "Dataset 缺少 acquisition target，无法自动采集。",
    "source_terminal_stale": "Dataset 来源已进入 stale 终态，不会自动重试。",
    "no_valid_fact": "Dataset 尚无可用于判断的有效事实。",
    "expected_fact_missing": "预期事实未在最大 freshness 预算内到达。",
    "expected_fact_delayed": "预期事实尚未到达，当前事实已延迟。",
    "last_expected_bar_present": "市场休市或维护期间，最近应有 bar 已存在。",
}

_HISTORY_REASON_MESSAGES = {
    "no_history_requirement": "该 Dataset 没有独立历史深度要求。",
    "configured_history_range_complete": "配置的历史区间已完整回填。",
    "history_backfill_incomplete": "配置的历史区间尚未完整，但已有部分有效历史事实。",
    "history_backfill_has_no_valid_fact": "配置的历史区间尚未形成有效历史事实。",
    "history_facts_missing": "该 feature 所需历史事实缺失。",
    "expected_history_window_present": "该 feature 的最小历史窗口已满足。",
    "expected_history_window_incomplete": "该 feature 的最小历史窗口尚未满足。",
    "history_backfill_terminal": "required 历史回填已进入终态，不会自动重试。",
}


def _dataset_current_reason(
    *,
    code: str,
    dataset_id: str,
    current_health: str,
    next_check_at_ms: int | None,
) -> dict[str, object]:
    healthy = current_health == "current"
    operator_action = code in {
        "acquisition_target_missing",
        "source_terminal_stale",
    }
    return macro_reason(
        code=code,
        message=_CURRENT_REASON_MESSAGES[code],
        impact="none" if healthy else "blocked" if current_health == "unavailable" else "limited",
        affected_dataset_ids=() if healthy else (dataset_id,),
        retryable=not healthy and not operator_action,
        recovery="none" if healthy else "operator_action" if operator_action else "automatic",
        next_action=(
            None
            if healthy
            else "由操作员检查并恢复该 Dataset；系统不会自动重试。"
            if code == "source_terminal_stale"
            else "创建 acquisition target 后重新投影模块。"
            if code == "acquisition_target_missing"
            else "等待下一次采集或派生投影，并复核该 Dataset。"
        ),
        next_check_at_ms=next_check_at_ms,
    )


def _dataset_history_reason(
    *,
    code: str,
    dataset_id: str,
    history_depth: str,
    required_for_history: bool,
    next_check_at_ms: int | None,
) -> dict[str, object]:
    if not required_for_history and history_depth != "not_required":
        complete = history_depth == "complete"
        return macro_reason(
            code=("optional_maximum_history_complete" if complete else "optional_maximum_history_incomplete"),
            message=(
                "可选最大公开历史已完整，仅作为审计信息。"
                if complete
                else "可选最大公开历史尚未完整，仅作为审计信息，不影响当前判断。"
            ),
            impact="none",
            retryable=False,
            recovery="none",
            next_check_at_ms=next_check_at_ms,
        )
    complete = history_depth in {"complete", "not_required"}
    terminal = code == "history_backfill_terminal"
    return macro_reason(
        code=code,
        message=_HISTORY_REASON_MESSAGES[code],
        impact="none" if complete else "blocked" if history_depth == "insufficient" else "limited",
        affected_dataset_ids=() if complete else (dataset_id,),
        retryable=not complete and not terminal,
        recovery="none" if complete else "operator_action",
        next_action=(
            None
            if complete
            else "检查 required 历史目标最后错误，修复来源后显式重新入队。"
            if terminal
            else "执行 tracefold macro backfill，补充满足该 feature 最小窗口的事实。"
        ),
        next_check_at_ms=next_check_at_ms,
    )


def _dataset_history_depth(
    spec: DatasetSpec,
    targets: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    *,
    active_backfills: list[dict[str, Any]],
) -> tuple[str, str]:
    backfills = [row for row in targets if str(row.get("clock_kind")) == "backfill"]
    if not _history_tracked(spec):
        return "not_required", "no_history_requirement"

    dated_facts = sorted(value for row in facts if (value := _fact_date(row)) is not None)
    semantic_starts = sorted(
        value for row in facts if (value := _history_start_date(row.get("semantic_history_start"))) is not None
    )
    expected_years = int(spec.metadata.get("history_years") or 5)
    history_start = min((*dated_facts, *semantic_starts), default=None)
    history_end = max(dated_facts, default=None)
    covered_days = (history_end - history_start).days if history_start is not None and history_end is not None else 0
    if covered_days >= expected_years * 365 - 14:
        return "complete", "expected_history_window_present"
    if any(str(row.get("status") or "") in {"stale", "unavailable", "invalid", "failed"} for row in backfills):
        return (
            ("partial", "history_backfill_terminal") if dated_facts else ("insufficient", "history_backfill_terminal")
        )
    if active_backfills:
        return (
            ("partial", "history_backfill_incomplete")
            if dated_facts
            else ("insufficient", "history_backfill_has_no_valid_fact")
        )
    if not dated_facts:
        return "insufficient", "history_facts_missing"
    return "partial", "expected_history_window_incomplete"


def _history_required(spec: DatasetSpec) -> bool:
    return spec.source_role == "history"


def _history_tracked(spec: DatasetSpec) -> bool:
    return _history_required(spec) or bool(spec.metadata.get("history_years"))


def _current_health(dataset_states: list[dict[str, Any]]) -> dict[str, Any]:
    tracked = [item for item in dataset_states if item["required_for_current"]]
    current_datasets = sum(item["current_health"] == "current" for item in tracked)
    state = "current" if current_datasets == len(tracked) else "unavailable" if current_datasets == 0 else "degraded"
    return {
        "state": state,
        "current_datasets": current_datasets,
        "tracked_datasets": len(tracked),
        "as_of_ms": max(
            (int(item["latest_received_at_ms"]) for item in tracked if item["latest_received_at_ms"] is not None),
            default=0,
        ),
        "groups": _health_groups(tracked),
    }


def _history_depth(dataset_states: list[dict[str, Any]]) -> dict[str, Any]:
    tracked = [state for state in dataset_states if state["required_for_history"]]
    if not tracked:
        state = "not_required"
    elif all(item["history_depth"] == "complete" for item in tracked):
        state = "complete"
    elif all(item["history_depth"] == "insufficient" for item in tracked):
        state = "insufficient"
    else:
        state = "partial"
    return {
        "state": state,
        "complete_datasets": sum(item["history_depth"] == "complete" for item in tracked),
        "tracked_datasets": len(tracked),
    }


def _backfill_execution(
    specs: tuple[DatasetSpec, ...],
    targets: list[dict[str, Any]],
) -> dict[str, Any]:
    dataset_ids = {spec.dataset_id for spec in specs if _history_required(spec)}
    backfills = [
        row
        for row in targets
        if str(row.get("dataset_id") or "") in dataset_ids and str(row.get("clock_kind") or "") == "backfill"
    ]
    statuses = [str(row.get("status") or "") for row in backfills]
    complete_targets = sum(status == "current" for status in statuses)
    failed_targets = sum(status in {"stale", "unavailable", "invalid", "failed"} for status in statuses)
    pending_targets = len(backfills) - complete_targets
    next_check_at_ms = min(
        (int(row["next_due_at_ms"]) for row in backfills if row.get("next_due_at_ms") is not None),
        default=None,
    )
    if not backfills:
        state = "not_required"
        reason = None
    elif pending_targets == 0:
        state = "complete"
        reason = None
    elif "claimed" in statuses:
        state = "running"
        reason = macro_reason(
            code="history_backfill_running",
            message="显式历史回填任务正在处理至少一个目标。",
            impact="limited",
            affected_dataset_ids=tuple(
                sorted({str(row["dataset_id"]) for row in backfills if row["status"] != "current"})
            ),
            retryable=True,
            recovery="automatic",
            next_action="等待当前租约完成后重新读取模块。",
            next_check_at_ms=next_check_at_ms,
        )
    elif failed_targets:
        state = "failed"
        reason = macro_reason(
            code="history_backfill_failed",
            message="至少一个历史回填目标已耗尽重试或进入不可用状态。",
            impact="limited",
            affected_dataset_ids=tuple(
                sorted(
                    {
                        str(row["dataset_id"])
                        for row in backfills
                        if str(row.get("status") or "") in {"stale", "unavailable", "invalid", "failed"}
                    }
                )
            ),
            retryable=False,
            recovery="operator_action",
            next_action="检查目标最后错误，修复来源后显式重新入队。",
        )
    elif any(status == "delayed" for status in statuses):
        state = "retry_wait"
        reason = macro_reason(
            code="history_backfill_retry_wait",
            message="历史回填目标正在等待下一次显式重试。",
            impact="limited",
            affected_dataset_ids=tuple(
                sorted({str(row["dataset_id"]) for row in backfills if row["status"] != "current"})
            ),
            retryable=True,
            recovery="operator_action",
            next_action="到达 next_check_at_ms 后重新执行 tracefold macro backfill。",
            next_check_at_ms=next_check_at_ms,
        )
    else:
        state = "queued"
        reason = macro_reason(
            code="history_backfill_queued",
            message="历史回填目标已入队，尚未执行显式维护任务。",
            impact="limited",
            affected_dataset_ids=tuple(
                sorted({str(row["dataset_id"]) for row in backfills if row["status"] != "current"})
            ),
            retryable=True,
            recovery="operator_action",
            next_action="执行 tracefold macro backfill。",
            next_check_at_ms=next_check_at_ms,
        )
    return {
        "state": state,
        "total_targets": len(backfills),
        "complete_targets": complete_targets,
        "pending_targets": pending_targets,
        "failed_targets": failed_targets,
        "next_check_at_ms": next_check_at_ms if state not in {"not_required", "complete", "failed"} else None,
        "reason": reason,
    }


def _health_groups(dataset_states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for state in dataset_states:
        grouped[str(state["health_group"])].append(state)
    output = []
    for group_id, rows in sorted(grouped.items()):
        output.append(
            {
                "group_id": group_id,
                "label": _HEALTH_GROUP_LABELS.get(group_id, group_id.replace("_", " ")),
                "current_health": _summary_axis(rows, "current_health", healthy="current"),
                "market_state": _summary_axis(rows, "market_state", healthy="open"),
                "source_state": _summary_axis(rows, "source_state", healthy="healthy"),
                "current_datasets": sum(row["current_health"] == "current" for row in rows),
                "tracked_datasets": len(rows),
            }
        )
    return output


def _summary_axis(rows: list[dict[str, Any]], key: str, *, healthy: str) -> str:
    values = {str(row[key]) for row in rows}
    if len(values) == 1:
        return next(iter(values))
    if healthy in values:
        return "mixed"
    return "mixed"


def _rates_payload(**groups: list[dict[str, Any]]) -> dict[str, Any]:
    series_rows = groups["series_rows"]
    curve_contract = calculate_curve_contract(series_rows)
    return {
        "decision": curve_contract["decision"],
        "curve": curve_contract["curve"],
        "policy_pricing": {
            "rates": _indicator_rows(
                series_rows,
                ("fred.effr", "fred.dfedtaru", "fred.dfedtarl", "fred.sofr", "fred.dgs2", "fred.dgs10"),
            ),
        },
        "fed": _fed_payload(
            groups["document_rows"],
            groups["role_rows"],
            groups["analysis_rows"],
        ),
        "positioning": _position_rows(groups["position_rows"], "cftc.tff.rates_positions"),
    }


def _economy_payload(**groups: list[dict[str, Any]]) -> dict[str, Any]:
    series_rows = groups["series_rows"]
    return {
        "inflation": {
            "indicators": _indicator_rows(
                series_rows,
                ("fred.cpiaucsl", "fred.cpilfesl", "fred.pcepi", "fred.pcepilfe"),
            ),
            "official_releases": _release_summaries(
                groups["release_rows"],
                (
                    "bls.cpi.release",
                    "bls.core_cpi.release",
                    "bea.pce.release",
                    "bea.core_pce.release",
                ),
            ),
        },
        "labor": {
            "indicators": _indicator_rows(series_rows, ("fred.payems", "fred.unrate", "fred.icsa")),
            "official_releases": _release_summaries(
                groups["release_rows"],
                ("bls.payrolls.release", "bls.unemployment.release"),
            ),
        },
        "growth": {
            "indicators": _indicator_rows(series_rows, ("fred.gdpc1", "fred.rsafs", "fred.indpro")),
            "official_releases": _release_summaries(
                groups["release_rows"],
                ("bea.gdp.release",),
            ),
        },
    }


def _liquidity_payload(**groups: list[dict[str, Any]]) -> dict[str, Any]:
    series_rows = groups["series_rows"]
    by_dataset = _series_by_dataset(series_rows)
    return {
        "balance_sheet": {
            "indicators": _indicator_rows(
                series_rows,
                ("fred.walcl", "fred.wrbwfrbl", "fred.wtregen", "fred.rrpontsyd"),
            ),
        },
        "funding": {
            "indicators": _indicator_rows(series_rows, ("fred.sofr", "fred.iorb")),
            "sofr_minus_iorb_bp_history": _paired_difference_history(
                by_dataset.get("fred.sofr", []),
                by_dataset.get("fred.iorb", []),
                multiplier=100,
            ),
        },
    }


def _credit_payload(**groups: list[dict[str, Any]]) -> dict[str, Any]:
    series_rows = groups["series_rows"]
    confirmation_pairs = (
        ("nasdaq.lqd.daily", "yfinance.lqd.intraday"),
        ("nasdaq.hyg.daily", "yfinance.hyg.intraday"),
    )
    confirmation_matrix = _cross_asset_return_matrix(
        groups["market_rows"],
        confirmation_pairs,
        group_by_history_dataset={
            dataset_id: ("credit_etf", "信用 ETF") for dataset_id, _price_dataset_id in confirmation_pairs
        },
    )
    ladder_ids = (
        "fred.bamlc0a0cm",
        "fred.bamlc0a4cbbb",
        "fred.bamlh0a1hybb",
        "fred.bamlh0a2hyb",
        "fred.bamlh0a3hyc",
    )
    ladder = _indicator_rows(series_rows, ladder_ids, percentile=True)
    levels = {row["dataset_id"]: row["latest_value"] for row in ladder}
    corporate_yields = _indicator_rows(
        series_rows,
        ("fred.bamlc0a0cmey", "fred.bamlh0a0hym2ey"),
        percentile=True,
    )
    bank_lending = _indicator_rows(
        series_rows,
        (
            "fred.drtscilm",
            "fred.drsdcilm",
            "fred.sublpdrcsn",
            "fred.sublpdrcdn",
            "fred.drtsclcc",
            "fred.demcc",
        ),
    )
    loan_quality = _indicator_rows(
        series_rows,
        (
            "fred.drblacbs",
            "fred.drcrelexfacbs",
            "fred.drcclacbs",
            "fred.corblacbs",
            "fred.corccacbs",
        ),
        percentile=True,
    )
    return {
        "cycle_dimensions": _credit_cycle_dimensions(
            ladder=ladder,
            corporate_yields=corporate_yields,
            bank_lending=bank_lending,
            loan_quality=loan_quality,
        ),
        "spread_ladder": {
            "rows": ladder,
            "tail_gap": _optional_difference(
                levels.get("fred.bamlh0a3hyc"),
                levels.get("fred.bamlh0a1hybb"),
                multiplier=100,
            ),
            "tail_gap_unit": "basis_points",
        },
        "funding_costs": {
            "corporate_yields": corporate_yields,
            "reference_rates": _indicator_rows(series_rows, ("fred.effr", "fred.dgs10")),
            "comparisons": calculate_funding_cost_comparisons(series_rows),
        },
        "bank_lending": {
            "indicators": bank_lending,
        },
        "loan_quality": {
            "indicators": loan_quality,
        },
        "confirmations": {
            "return_matrix": confirmation_matrix,
            "source_identity": [
                _cross_asset_identity_row(
                    display_order=index,
                    evidence_kind="credit_etf",
                    label=row["label"],
                    selection_policy=row["selection_policy"],
                    sources=[row["latest_source"], row["return_source"]],
                    symbol=row["symbol"],
                )
                for index, row in enumerate(confirmation_matrix, start=1)
            ],
            "positions": _position_rows(groups["position_rows"], "cftc.tff.credit_positions"),
        },
    }


def _credit_cycle_dimensions(
    *,
    ladder: list[dict[str, Any]],
    corporate_yields: list[dict[str, Any]],
    bank_lending: list[dict[str, Any]],
    loan_quality: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    spread_changes = [float(row["change_1m"]) for row in ladder if row.get("change_1m") is not None]
    spread_percentiles = [float(row["percentile"]) for row in ladder if row.get("percentile") is not None]
    average_spread_change = _average(spread_changes)
    max_spread_percentile = max(spread_percentiles, default=None)
    spread_conflicts: list[str] = []
    if (
        average_spread_change is not None
        and max_spread_percentile is not None
        and average_spread_change > 0.1
        and max_spread_percentile < 40
    ):
        spread_conflicts.append("利差仍处低分位，但近月正在走阔")
    if (
        average_spread_change is not None
        and max_spread_percentile is not None
        and average_spread_change < -0.1
        and max_spread_percentile >= 80
    ):
        spread_conflicts.append("利差处高分位，但近月正在收窄")
    if not ladder:
        spread_state, spread_driver = "insufficient", "评级梯级事实不足"
    elif max_spread_percentile is not None and max_spread_percentile >= 80:
        spread_state, spread_driver = "stressed", "至少一个评级利差处于实际历史高分位"
    elif average_spread_change is not None and average_spread_change > 0.1:
        spread_state, spread_driver = "tightening", "评级梯级近月平均走阔"
    elif average_spread_change is not None and average_spread_change < -0.1:
        spread_state, spread_driver = "easing", "评级梯级近月平均收窄"
    else:
        spread_state, spread_driver = "neutral", "评级梯级水平与速度未形成压力信号"

    funding_percentiles = [float(row["percentile"]) for row in corporate_yields if row.get("percentile") is not None]
    max_funding_percentile = max(funding_percentiles, default=None)
    if not corporate_yields:
        funding_state, funding_driver = "insufficient", "IG/HY有效收益率事实不足"
    elif max_funding_percentile is not None and max_funding_percentile >= 80:
        funding_state, funding_driver = "expensive", "公司债绝对收益率处于实际历史高分位"
    elif max_funding_percentile is not None and max_funding_percentile <= 20:
        funding_state, funding_driver = "cheap", "公司债绝对收益率处于实际历史低分位"
    else:
        funding_state, funding_driver = "normal", "公司债绝对融资成本处于历史中部"

    standards = _indicator_values(
        bank_lending,
        ("fred.drtscilm", "fred.sublpdrcsn", "fred.drtsclcc"),
    )
    demand = _indicator_values(
        bank_lending,
        ("fred.drsdcilm", "fred.sublpdrcdn", "fred.demcc"),
    )
    average_standards = _average(standards)
    average_demand = _average(demand)
    supply_conflicts: list[str] = []
    if average_standards is not None and average_demand is not None:
        if average_standards > 10 and average_demand > 10:
            supply_conflicts.append("银行标准收紧与贷款需求增强同时出现")
        if average_standards < -10 and average_demand < -10:
            supply_conflicts.append("银行标准放松但贷款需求仍弱")
    if average_standards is None or average_demand is None:
        supply_state, supply_driver = "insufficient", "C&I、CRE、消费标准或需求尚未齐全"
    elif average_standards > 10:
        supply_state, supply_driver = "restrictive", "三类贷款标准平均处于净收紧"
    elif average_demand < -10:
        supply_state, supply_driver = "weak_demand", "三类贷款需求平均偏弱"
    elif average_standards < -10:
        supply_state, supply_driver = "easing", "三类贷款标准平均处于净放松"
    else:
        supply_state, supply_driver = "neutral", "银行供给与需求未形成一致压力"

    quality_changes = [float(row["change_1m"]) for row in loan_quality if row.get("change_1m") is not None]
    quality_percentiles = [float(row["percentile"]) for row in loan_quality if row.get("percentile") is not None]
    average_quality_change = _average(quality_changes)
    max_quality_percentile = max(quality_percentiles, default=None)
    quality_conflicts: list[str] = []
    if (
        average_quality_change is not None
        and max_quality_percentile is not None
        and average_quality_change < 0
        and max_quality_percentile >= 80
    ):
        quality_conflicts.append("贷款损失指标仍处高分位，但近期有所改善")
    if not loan_quality:
        quality_state, quality_driver = "insufficient", "逾期率与核销率事实不足"
    elif max_quality_percentile is not None and max_quality_percentile >= 80:
        quality_state, quality_driver = "stressed", "至少一项贷款损失指标处于实际历史高分位"
    elif average_quality_change is not None and average_quality_change > 0.05:
        quality_state, quality_driver = "deteriorating", "逾期率或核销率近月平均上升"
    elif average_quality_change is not None and average_quality_change < -0.05:
        quality_state, quality_driver = "improving", "逾期率与核销率近月平均下降"
    else:
        quality_state, quality_driver = "stable", "实现贷款质量未出现一致恶化"

    return [
        _credit_dimension(
            "spread_level_velocity",
            "利差水平与速度",
            spread_state,
            spread_driver,
            [str(row["dataset_id"]) for row in ladder],
            spread_conflicts,
        ),
        _credit_dimension(
            "funding_cost",
            "绝对融资成本",
            funding_state,
            funding_driver,
            [str(row["dataset_id"]) for row in corporate_yields],
            [],
        ),
        _credit_dimension(
            "credit_supply",
            "银行供给与需求",
            supply_state,
            supply_driver,
            [str(row["dataset_id"]) for row in bank_lending],
            supply_conflicts,
        ),
        _credit_dimension(
            "credit_quality",
            "实现信用质量",
            quality_state,
            quality_driver,
            [str(row["dataset_id"]) for row in loan_quality],
            quality_conflicts,
        ),
    ]


def _credit_dimension(
    dimension_id: str,
    label: str,
    state: str,
    driver: str,
    evidence_dataset_ids: list[str],
    conflicts: list[str],
) -> dict[str, Any]:
    return {
        "dimension_id": dimension_id,
        "label": label,
        "state": state,
        "driver": driver,
        "evidence_dataset_ids": evidence_dataset_ids,
        "conflicts": conflicts,
    }


def _indicator_values(
    rows: list[dict[str, Any]],
    dataset_ids: tuple[str, ...],
) -> list[float]:
    by_id = {str(row["dataset_id"]): row for row in rows}
    return [float(by_id[dataset_id]["latest_value"]) for dataset_id in dataset_ids if dataset_id in by_id]


def _average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _volatility_payload(**groups: list[dict[str, Any]]) -> dict[str, Any]:
    series_rows = groups["series_rows"]
    vix = _series_by_dataset(series_rows).get("fred.vixcls", [])
    vxv = _series_by_dataset(series_rows).get("fred.vxvcls", [])
    cross_asset_ids = ("fred.vxncls", "fred.gvzcls", "fred.ovxcls")
    normalized_rows = _normalized_indicator_history(
        series_rows,
        cross_asset_ids,
        {
            "fred.vxncls": "VXN",
            "fred.gvzcls": "GVZ",
            "fred.ovxcls": "OVX",
        },
    )
    return {
        "term_structure": {
            "spot_and_three_month": _indicator_rows(series_rows, ("fred.vixcls", "fred.vxvcls")),
            "spread_history": _paired_difference_history(vix, vxv),
            "official_vx_curve": _settlement_rows(
                groups["settlement_rows"],
                "cboe.cfe.vx.settlement",
            ),
        },
        "cross_asset_implied": {
            "indicators": _indicator_rows(
                series_rows,
                cross_asset_ids,
            ),
            "normalized_groups": _normalized_groups(
                normalized_rows,
                (
                    (
                        "cross_asset_implied_volatility",
                        "跨资产隐含波动率",
                        cross_asset_ids,
                    ),
                ),
                symbols_by_dataset={
                    "fred.vxncls": "VXN",
                    "fred.gvzcls": "GVZ",
                    "fred.ovxcls": "OVX",
                },
            ),
        },
    }


def _cross_asset_payload(**groups: list[dict[str, Any]]) -> dict[str, Any]:
    market_rows = groups["market_rows"]
    series_rows = groups["series_rows"]
    symbol_by_dataset = {
        dataset_id: str(DATASET_REGISTRY[dataset_id].symbol or dataset_id) for dataset_id in _ETF_DAILY_IDS
    }
    comparison = calculate_market_comparison(
        market_rows,
        _ETF_DAILY_IDS,
        symbol_by_dataset,
    )
    return_matrix = _cross_asset_return_matrix(
        market_rows,
        _ETF_DATASETS,
        group_by_history_dataset={
            dataset_id: (group_id, group_label)
            for group_id, group_label, dataset_ids in _CROSS_ASSET_NORMALIZED_GROUPS
            for dataset_id in dataset_ids
        },
    )
    futures_return_matrix = _cross_asset_return_matrix(
        market_rows,
        _FUTURES_DATASETS,
        group_by_history_dataset={dataset_id: ("major_futures", "主要连续期货") for dataset_id in _FUTURES_DAILY_IDS},
    )
    market_facts = {
        str(item["dataset_id"]): item
        for item in calculate_market_statistics(
            market_rows,
            (
                "binance.btcusdt.spot",
                "yfinance.btc_yahoo.intraday",
                "yfinance.vix_index.intraday",
            ),
        )
    }
    wti = _indicator_rows(series_rows, ("fred.dcoilwtico",))
    vix_daily = _indicator_rows(series_rows, ("fred.vixcls",))
    source_identity = [
        *[
            _cross_asset_identity_row(
                display_order=index,
                evidence_kind="etf",
                label=row["label"],
                selection_policy=row["selection_policy"],
                sources=[row["latest_source"], row["return_source"]],
                symbol=row["symbol"],
            )
            for index, row in enumerate(return_matrix, start=1)
        ],
        _cross_asset_identity_row(
            display_order=11,
            evidence_kind="official_benchmark",
            label="WTI Cushing 现货",
            selection_policy="decision_primary_only_no_fallback",
            sources=[
                _cross_asset_source_selection(
                    "fred.dcoilwtico",
                    wti[0] if wti else None,
                )
            ],
            symbol="WTI",
        ),
        _cross_asset_identity_row(
            display_order=12,
            evidence_kind="crypto",
            label="Bitcoin / USD",
            selection_policy="decision_primary_only_no_fallback",
            sources=[
                _cross_asset_source_selection(
                    "binance.btcusdt.spot",
                    market_facts.get("binance.btcusdt.spot"),
                ),
                _cross_asset_source_selection(
                    "yfinance.btc_yahoo.intraday",
                    market_facts.get("yfinance.btc_yahoo.intraday"),
                ),
            ],
            symbol="BTC",
        ),
        _cross_asset_identity_row(
            display_order=13,
            evidence_kind="volatility",
            label="Cboe Volatility Index",
            selection_policy="decision_primary_only_no_fallback",
            sources=[
                _cross_asset_source_selection(
                    "fred.vixcls",
                    vix_daily[0] if vix_daily else None,
                ),
                _cross_asset_source_selection(
                    "yfinance.vix_index.intraday",
                    market_facts.get("yfinance.vix_index.intraday"),
                ),
            ],
            symbol="VIX",
        ),
    ]
    return {
        "assets": {
            "return_matrix": return_matrix,
            "normalized_groups": _normalized_groups(
                comparison["normalized"],
                _CROSS_ASSET_NORMALIZED_GROUPS,
            ),
            "source_identity": source_identity,
        },
        "correlations": comparison["correlations"],
        "futures": {
            "return_matrix": futures_return_matrix,
            "positions": _position_rows(groups["position_rows"], "cftc.tff.cross_asset_positions"),
        },
    }


def _cross_asset_return_matrix(
    market_rows: list[dict[str, Any]],
    dataset_pairs: tuple[tuple[str, str], ...],
    *,
    group_by_history_dataset: Mapping[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    dataset_ids = tuple(dataset_id for pair in dataset_pairs for dataset_id in pair)
    facts = {str(item["dataset_id"]): item for item in calculate_market_statistics(market_rows, dataset_ids)}
    rows = []
    for display_order, (history_dataset_id, price_dataset_id) in enumerate(
        dataset_pairs,
        start=1,
    ):
        history_spec = DATASET_REGISTRY[history_dataset_id]
        price_spec = DATASET_REGISTRY[price_dataset_id]
        group_id, group_label = group_by_history_dataset[history_dataset_id]
        rows.append(
            {
                "display_order": display_order,
                "group_id": group_id,
                "group_label": group_label,
                "symbol": str(price_spec.symbol or history_spec.symbol or history_dataset_id),
                "label": price_spec.label,
                "identity_policy": "separate_source_facts_no_blend",
                "selection_policy": "intraday_latest_and_daily_returns_exact",
                "latest_source": _cross_asset_source_selection(
                    price_dataset_id,
                    facts.get(price_dataset_id),
                ),
                "return_source": _cross_asset_source_selection(
                    history_dataset_id,
                    facts.get(history_dataset_id),
                ),
            }
        )
    return rows


def _cross_asset_source_selection(
    dataset_id: str,
    fact: dict[str, Any] | None,
) -> dict[str, Any]:
    spec = DATASET_REGISTRY[dataset_id]
    return {
        "dataset_id": dataset_id,
        "label": spec.label,
        "source_role": spec.source_role,
        "fact": fact,
    }


def _cross_asset_identity_row(
    *,
    display_order: int,
    evidence_kind: str,
    label: str,
    selection_policy: str,
    sources: list[dict[str, Any]],
    symbol: str,
) -> dict[str, Any]:
    return {
        "display_order": display_order,
        "symbol": symbol,
        "label": label,
        "evidence_kind": evidence_kind,
        "identity_policy": "separate_source_facts_no_blend",
        "selection_policy": selection_policy,
        "sources": sources,
    }


def _normalized_groups(
    rows: list[dict[str, Any]],
    groups: tuple[tuple[str, str, tuple[str, ...]], ...],
    *,
    symbols_by_dataset: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    points_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        points_by_symbol[str(row["symbol"])].append(
            {
                "date": str(row["date"]),
                "normalized_value": float(row["normalized_value"]),
            }
        )
    return [
        {
            "display_order": group_order,
            "group_id": group_id,
            "label": group_label,
            "series": [
                {
                    "display_order": series_order,
                    "symbol": (
                        symbols_by_dataset[dataset_id]
                        if symbols_by_dataset is not None
                        else str(DATASET_REGISTRY[dataset_id].symbol or dataset_id)
                    ),
                    "label": DATASET_REGISTRY[dataset_id].label,
                    "source": _cross_asset_source_selection(dataset_id, None),
                    "points": points_by_symbol.get(
                        (
                            symbols_by_dataset[dataset_id]
                            if symbols_by_dataset is not None
                            else str(DATASET_REGISTRY[dataset_id].symbol or dataset_id)
                        ),
                        [],
                    ),
                }
                for series_order, dataset_id in enumerate(dataset_ids, start=1)
            ],
        }
        for group_order, (group_id, group_label, dataset_ids) in enumerate(groups, start=1)
    ]


def _fed_payload(
    document_rows: list[dict[str, Any]],
    role_rows: list[dict[str, Any]],
    analysis_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    role_rows = effective_roster_rows(role_rows)
    rows = sorted(
        (
            row
            for row in document_rows
            if row["dataset_id"]
            in {
                "federal_reserve.fomc.documents",
                "federal_reserve.board.speeches",
                "federal_reserve.reserve_bank.speeches",
            }
        ),
        key=lambda row: (int(row["published_at_ms"]), str(row["document_id"])),
        reverse=True,
    )
    latest_analysis_by_document: dict[str, dict[str, Any]] = {}
    for analysis in analysis_rows:
        if str(analysis.get("reviewer_disposition")) != "pass":
            continue
        document_id = str(analysis["document_id"])
        current = latest_analysis_by_document.get(document_id)
        if current is None or int(analysis["created_at_ms"]) > int(current["created_at_ms"]):
            latest_analysis_by_document[document_id] = analysis
    all_events = []
    for row in rows:
        document_analysis = latest_analysis_by_document.get(str(row["document_id"]))
        analysis_payload = document_analysis.get("analysis_json") if document_analysis is not None else None
        role_context = (
            analysis_payload.get("roster_context")
            if isinstance(analysis_payload, dict) and isinstance(analysis_payload.get("roster_context"), dict)
            else match_effective_role(
                (row.get("metadata_json") or {}).get("speaker_name"),
                effective_date=row["effective_date"],
                role_rows=role_rows,
            )
        )
        all_events.append(
            {
                "document_id": row["document_id"],
                "document_type": row["document_type"],
                "title": row["title"],
                "effective_date": str(row["effective_date"]),
                "published_at_ms": int(row["published_at_ms"]),
                "source_url": row["source_url"],
                "speaker_name": (row.get("metadata_json") or {}).get("speaker_name"),
                "official_id": role_context.get("official_id") if role_context else None,
                "role_title": role_context.get("role_title") if role_context else None,
                "fomc_voter": bool(role_context.get("fomc_voter")) if role_context else None,
                "analysis": {
                    "state": ("analyzed" if document_analysis is not None else "not_analyzed"),
                    "policy_relevance": (
                        str(document_analysis["policy_relevance"]) if document_analysis is not None else "unknown"
                    ),
                    "stance": (str(document_analysis["stance"]) if document_analysis is not None else "no_call"),
                    "confidence": (document_analysis.get("confidence") if document_analysis is not None else None),
                    "change_from_prior": (
                        analysis_payload.get("change_from_prior") if isinstance(analysis_payload, dict) else None
                    ),
                    "rationale": (analysis_payload.get("rationale") if isinstance(analysis_payload, dict) else None),
                    "evidence": (analysis_payload.get("evidence", []) if isinstance(analysis_payload, dict) else []),
                    "analysis_id": (document_analysis.get("analysis_id") if document_analysis is not None else None),
                    "model_name": (document_analysis.get("model_name") if document_analysis is not None else None),
                    "prompt_version": (
                        document_analysis.get("prompt_version") if document_analysis is not None else None
                    ),
                    "reviewer_disposition": (
                        document_analysis.get("reviewer_disposition") if document_analysis is not None else None
                    ),
                },
            }
        )
    institutional = next(
        (
            event
            for event in all_events
            if event["document_type"] == "statement"
            and event["analysis"]["state"] == "analyzed"
            and event["analysis"]["policy_relevance"] == "policy_signal"
        ),
        None,
    )
    reference_date = max((row["effective_date"] for row in rows), default=None)
    communication_cutoff = reference_date - timedelta(days=90) if isinstance(reference_date, date) else None
    speech_analyses = [
        event
        for event in all_events
        if event["document_type"] == "speech"
        and event["analysis"]["state"] == "analyzed"
        and (communication_cutoff is None or date.fromisoformat(str(event["effective_date"])) >= communication_cutoff)
    ]
    policy_speeches = [
        event
        for event in speech_analyses
        if event["analysis"]["policy_relevance"] == "policy_signal" and event["official_id"] is not None
    ]
    current_roles: dict[str, dict[str, Any]] = {}
    if isinstance(reference_date, date):
        eligible_starts = [
            role["effective_start"]
            for role in role_rows
            if role.get("effective_start") is not None and role["effective_start"] <= reference_date
        ]
        active_start = max(eligible_starts, default=None)
        for role in role_rows:
            if role["effective_start"] != active_start:
                continue
            if role["effective_end"] is not None and role["effective_end"] < reference_date:
                continue
            official_id = str(role["official_id"])
            current = current_roles.get(official_id)
            if current is None or role["effective_start"] > current["effective_start"]:
                current_roles[official_id] = role
    latest_policy_speech_by_official: dict[str, dict[str, Any]] = {}
    for event in policy_speeches:
        official_id = str(event["official_id"])
        if official_id not in latest_policy_speech_by_official:
            latest_policy_speech_by_official[official_id] = event
    stance_names = ("hawkish", "neutral", "dovish", "mixed")
    return {
        "institutional_stance": {
            "state": "current" if institutional is not None else "no_call",
            "direction": institutional["analysis"]["stance"] if institutional else "no_call",
            "change_from_prior": (institutional["analysis"]["change_from_prior"] if institutional else "no_call"),
            "analysis_id": institutional["analysis"]["analysis_id"] if institutional else None,
            "reason": (
                institutional["analysis"]["rationale"] if institutional else "尚未发布通过独立审阅的 FOMC 声明分析。"
            ),
        },
        "officials_distribution": {
            "state": "current" if speech_analyses else "no_call",
            "window_days": 90,
            "as_of": str(reference_date) if reference_date else None,
            "stance_event_counts": {
                stance: sum(event["analysis"]["stance"] == stance for event in policy_speeches)
                for stance in stance_names
            },
            "stance_unique_official_counts": {
                stance: sum(
                    event["analysis"]["stance"] == stance for event in latest_policy_speech_by_official.values()
                )
                for stance in stance_names
            },
            "not_policy_signal_event_count": sum(
                event["analysis"]["policy_relevance"] == "not_policy_signal" for event in speech_analyses
            ),
            "uncertain_event_count": sum(
                event["analysis"]["policy_relevance"] == "uncertain" for event in speech_analyses
            ),
            "analyzed_event_count": len(speech_analyses),
            "unique_official_count": len(latest_policy_speech_by_official),
        },
        "timeline": all_events[:80],
        "roster": {
            "state": "current" if current_roles else "unavailable",
            "reason": None if current_roles else "effective_dated_roster_not_ingested",
            "officials": [
                {
                    "official_id": role["official_id"],
                    "official_name": role["official_name"],
                    "role_title": role["role_title"],
                    "organization": role["organization"],
                    "effective_start": str(role["effective_start"]),
                    "effective_end": str(role["effective_end"]) if role["effective_end"] else None,
                    "fomc_participant": bool(role["fomc_participant"]),
                    "fomc_voter": bool(role["fomc_voter"]),
                    "source_url": role["source_url"],
                    "role_fact_id": role["role_fact_id"],
                }
                for role in sorted(
                    current_roles.values(),
                    key=lambda item: (not bool(item["fomc_voter"]), str(item["official_name"])),
                )
            ],
        },
    }


def _indicator_rows(
    series_rows: list[dict[str, Any]],
    dataset_ids: tuple[str, ...],
    *,
    percentile: bool = False,
) -> list[dict[str, Any]]:
    calculated = calculate_series_statistics(
        series_rows,
        dataset_ids,
        percentile_dataset_ids=frozenset(dataset_ids) if percentile else frozenset(),
    )
    output: list[dict[str, Any]] = []
    for item in calculated:
        dataset_id = str(item["dataset_id"])
        spec = DATASET_REGISTRY[dataset_id]
        output.append({**item, "label": spec.label, "unit": spec.unit})
    return output


def _series_by_dataset(series_rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_dataset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in series_rows:
        if row.get("value_numeric") is not None:
            by_dataset[str(row["dataset_id"])].append(row)
    for rows in by_dataset.values():
        rows.sort(key=lambda row: (row["reference_date"], row["vintage_date"], int(row["received_at_ms"])))
    return dict(by_dataset)


def _position_rows(rows: list[dict[str, Any]], dataset_id: str) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["dataset_id"] != dataset_id:
            continue
        contract = str(row["contract_code"])
        if contract not in latest or row["report_date"] > latest[contract]["report_date"]:
            latest[contract] = row
    return [
        {
            "contract_code": row["contract_code"],
            "contract_name": row["contract_name"],
            "report_date": str(row["report_date"]),
            "leveraged_net_pct_oi": float(row["leveraged_net_pct_oi"]),
            "asset_manager_net_pct_oi": float(row["asset_manager_net_pct_oi"]),
            "dealer_net_pct_oi": float(row["dealer_net_pct_oi"]),
            "source_url": row["source_url"],
        }
        for row in sorted(latest.values(), key=lambda item: str(item["contract_code"]))
    ]


def _settlement_rows(rows: list[dict[str, Any]], dataset_id: str) -> list[dict[str, Any]]:
    candidates = [row for row in rows if row["dataset_id"] == dataset_id]
    latest_trade_date = max(
        (row["trade_date"] for row in candidates),
        default=None,
    )
    matching = sorted(
        (row for row in candidates if row["trade_date"] == latest_trade_date),
        key=lambda row: (
            row["contract_expiration_date"],
            str(row["contract_code"]),
        ),
    )
    return [
        {
            "trade_date": str(row["trade_date"]),
            "contract_code": row["contract_code"],
            "contract_expiration_date": str(row["contract_expiration_date"]),
            "settlement_price": float(row["settlement_price"]),
            "open_interest": row.get("open_interest"),
            "volume": row.get("volume"),
            "published_at_ms": (int(row["published_at_ms"]) if row.get("published_at_ms") is not None else None),
            "received_at_ms": int(row["received_at_ms"]),
            "source_url": row["source_url"],
        }
        for row in matching[:30]
    ]


def _current_settlement_revisions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    revisions: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("fact_schema_version") != "market_settlement_v2":
            continue
        key = (
            str(row["dataset_id"]),
            str(row.get("instrument_id") or ""),
            str(row["trade_date"]),
            str(row["contract_code"]),
        )
        revisions[key].append(row)
    current = []
    for candidates in revisions.values():
        ordered = sorted(
            candidates,
            key=lambda row: (
                int(row.get("received_at_ms") or 0),
                int(row.get("published_at_ms") or -1),
                str(row.get("settlement_id") or row.get("fact_hash") or ""),
            ),
        )
        latest = ordered[-1]
        latest_clock = (
            int(latest.get("received_at_ms") or 0),
            int(latest.get("published_at_ms") or -1),
        )
        tied = [
            row
            for row in ordered
            if (
                int(row.get("received_at_ms") or 0),
                int(row.get("published_at_ms") or -1),
            )
            == latest_clock
        ]
        if len({float(row["settlement_price"]) for row in tied}) > 1:
            continue
        current.append(latest)
    return current


def _release_summaries(rows: list[dict[str, Any]], dataset_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    latest_by_period: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        dataset_id = str(row["dataset_id"])
        if dataset_id not in dataset_ids:
            continue
        reference_period = str(row["reference_period"])
        current = latest_by_period[dataset_id].get(reference_period)
        if current is None or _release_order(row) > _release_order(current):
            latest_by_period[dataset_id][reference_period] = row
    output = []
    for dataset_id in sorted(latest_by_period):
        observations = sorted(
            latest_by_period[dataset_id].values(),
            key=_release_order,
            reverse=True,
        )[:12]
        if not observations:
            continue
        output.append(
            {
                "dataset_id": dataset_id,
                "label": DATASET_REGISTRY[dataset_id].label,
                **_release_observation(observations[0]),
                "observations": [_release_observation(row) for row in observations],
            }
        )
    return output


def _release_observation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "reference_period": row["reference_period"],
        "scheduled_at_ms": (int(row["scheduled_at_ms"]) if row.get("scheduled_at_ms") is not None else None),
        "actual_value": row["actual_value"],
        "estimate_value": row["estimate_value"],
        "prior_value": row["prior_value"],
        "revised_prior_value": row["revised_prior_value"],
        **_release_delta(row),
        "unit": row["unit"],
        "published_at_ms": (int(row["published_at_ms"]) if row.get("published_at_ms") is not None else None),
        "received_at_ms": int(row["received_at_ms"]),
        "source_url": row["source_url"],
    }


def _paired_difference_history(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
    *,
    multiplier: float = 1,
) -> list[dict[str, Any]]:
    left_by_date = {row["reference_date"]: float(row["value_numeric"]) for row in left}
    right_by_date = {row["reference_date"]: float(row["value_numeric"]) for row in right}
    return [
        {
            "date": value.isoformat(),
            "value": round((left_by_date[value] - right_by_date[value]) * multiplier, 6),
        }
        for value in sorted(set(left_by_date) & set(right_by_date))
    ][-500:]


def _normalized_indicator_history(
    series_rows: list[dict[str, Any]],
    dataset_ids: tuple[str, ...],
    symbols: Mapping[str, str],
) -> list[dict[str, Any]]:
    by_dataset = _series_by_dataset(series_rows)
    output: list[dict[str, Any]] = []
    for dataset_id in dataset_ids:
        rows = by_dataset.get(dataset_id, [])[-500:]
        if not rows:
            continue
        base = float(rows[0]["value_numeric"])
        if base == 0:
            continue
        output.extend(
            {
                "symbol": symbols[dataset_id],
                "date": str(row["reference_date"]),
                "normalized_value": round(float(row["value_numeric"]) / base * 100, 4),
            }
            for row in rows
        )
    return output


def _top_changes(
    specs: tuple[DatasetSpec, ...],
    series_rows: list[dict[str, Any]],
    market_rows: list[dict[str, Any]],
    settlement_rows: list[dict[str, Any]],
    release_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows_by_dataset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in (*series_rows, *market_rows, *settlement_rows, *release_rows):
        rows_by_dataset[str(row["dataset_id"])].append(row)
    changes = []
    for spec in specs:
        rows = sorted(rows_by_dataset.get(spec.dataset_id, []), key=_fact_order)
        if not rows or _fact_value(rows[-1]) is None:
            continue
        latest = float(_fact_value(rows[-1]) or 0)
        cadence, metrics, metric_unit = _natural_change_metrics(spec, rows)
        release_delta = _release_delta(rows[-1]) if spec.fact_family == "release" else {}
        metrics = {**metrics, **release_delta}
        non_null_metrics = {key: value for key, value in metrics.items() if value is not None}
        comparable_values = [abs(value) for value in non_null_metrics.values()]
        strongest_name = max(
            non_null_metrics,
            key=lambda key: abs(non_null_metrics[key]),
            default=None,
        )
        strongest_value = non_null_metrics[strongest_name] if strongest_name is not None else None
        standardized_magnitude = round(
            max(comparable_values, default=0.0) / (25.0 if metric_unit == "basis_points" else 2.0),
            6,
        )
        surprise_magnitude = abs(float(release_delta.get("surprise") or 0.0))
        revision_magnitude = abs(float(release_delta.get("revision") or 0.0))
        decision_relevance = int(spec.metadata.get("importance_tier") or (2 if spec.critical else 1))
        trust_rank = {"official": 3, "exchange": 2, "untrusted_proxy": 1}[spec.trust_tier]
        freshness_ms = max(0, _fact_clock_ms(rows[-1]))
        changes.append(
            {
                "dataset_id": spec.dataset_id,
                "concept_id": spec.concept_id,
                "source_role": spec.source_role,
                "label": spec.label,
                "as_of": _fact_reference(rows[-1]),
                "value": latest,
                "unit": spec.unit,
                "cadence": cadence,
                "metrics": metrics,
                "metric_unit": metric_unit,
                "primary_change": strongest_value,
                "importance_factors": {
                    "standardized_magnitude": standardized_magnitude,
                    "surprise_magnitude": surprise_magnitude,
                    "revision_magnitude": revision_magnitude,
                    "decision_relevance": decision_relevance,
                    "trust_tier": spec.trust_tier,
                    "fact_clock_ms": freshness_ms,
                },
                "importance_explanation": (
                    (
                        f"{_change_metric_label(strongest_name)}"
                        f"{_reader_value(strongest_value, metric_unit, signed=True)}；"
                        "按自然频率变化幅度、决策关联和来源质量排序；"
                        f"{_trust_tier_label(spec.trust_tier)}。"
                    )
                    if strongest_name is not None and strongest_value is not None
                    else "可比变化窗口不足"
                ),
                "source_url": rows[-1]["source_url"],
                "_sort_key": (
                    standardized_magnitude,
                    surprise_magnitude,
                    revision_magnitude,
                    decision_relevance,
                    freshness_ms,
                    trust_rank,
                    spec.dataset_id,
                ),
            }
        )
    ranked = sorted(changes, key=lambda item: item["_sort_key"], reverse=True)
    return [
        {
            **{key: value for key, value in item.items() if key != "_sort_key"},
            "importance_rank": rank,
        }
        for rank, item in enumerate(ranked, start=1)
    ]


def _natural_change_metrics(
    spec: DatasetSpec,
    rows: list[dict[str, Any]],
) -> tuple[str, dict[str, float | None], str]:
    calculation = natural_change_calculation(spec.dataset_id)
    points = _observation_points(rows)
    if not points:
        return calculation.cadence, {}, calculation.output_unit
    use_basis_points = spec.unit == "percent"
    metric_unit = calculation.output_unit
    if calculation.cadence in {"intraday", "daily"}:
        return (
            "daily",
            {
                ("change_1d_bp" if use_basis_points else "return_1d_pct"): _window_change(
                    points,
                    days=1,
                    basis_points=use_basis_points,
                    max_lag_days=calculation.max_gap_days,
                ),
                ("change_1w_bp" if use_basis_points else "return_1w_pct"): _window_change(
                    points,
                    days=7,
                    basis_points=use_basis_points,
                    max_lag_days=calculation.max_gap_days,
                ),
                ("change_1m_bp" if use_basis_points else "return_1m_pct"): _window_change(
                    points,
                    days=30,
                    basis_points=use_basis_points,
                    max_lag_days=calculation.max_gap_days,
                ),
            },
            metric_unit,
        )
    if calculation.cadence == "weekly":
        return (
            "weekly",
            {
                ("change_wow_bp" if use_basis_points else "change_wow_pct"): _window_change(
                    points,
                    days=7,
                    basis_points=use_basis_points,
                    max_lag_days=calculation.max_gap_days,
                ),
                ("change_4w_bp" if use_basis_points else "change_4w_pct"): _window_change(
                    points,
                    days=28,
                    basis_points=use_basis_points,
                    max_lag_days=calculation.max_gap_days,
                ),
            },
            metric_unit,
        )
    if calculation.cadence == "monthly":
        return (
            "monthly",
            {
                ("mom_bp" if use_basis_points else "mom_pct"): _period_change(
                    points,
                    periods=1,
                    basis_points=use_basis_points,
                    months_per_period=1,
                ),
                ("change_3m_bp" if use_basis_points else "three_month_annualized_pct"): _period_change(
                    points,
                    periods=3,
                    basis_points=use_basis_points,
                    months_per_period=1,
                    annualization_periods=4 if not use_basis_points else None,
                ),
                ("yoy_bp" if use_basis_points else "yoy_pct"): _period_change(
                    points,
                    periods=12,
                    basis_points=use_basis_points,
                    months_per_period=1,
                ),
            },
            metric_unit,
        )
    if calculation.cadence == "quarterly":
        return (
            "quarterly",
            {
                ("qoq_bp" if use_basis_points else "qoq_annualized_pct"): _period_change(
                    points,
                    periods=1,
                    basis_points=use_basis_points,
                    months_per_period=3,
                    annualization_periods=4 if not use_basis_points else None,
                ),
                ("yoy_bp" if use_basis_points else "yoy_pct"): _period_change(
                    points,
                    periods=4,
                    basis_points=use_basis_points,
                    months_per_period=3,
                ),
            },
            metric_unit,
        )
    return calculation.cadence, {}, metric_unit


def _observation_points(rows: list[dict[str, Any]]) -> list[tuple[date, float]]:
    current_by_date: dict[date, dict[str, Any]] = {}
    for row in rows:
        observed_on = _fact_date(row)
        value = _fact_value(row)
        if observed_on is None or value is None:
            continue
        current = current_by_date.get(observed_on)
        if current is None or _fact_order(row) > _fact_order(current):
            current_by_date[observed_on] = row
    return [(observed_on, float(_fact_value(row) or 0)) for observed_on, row in sorted(current_by_date.items())]


def _window_change(
    points: list[tuple[date, float]],
    *,
    days: int,
    basis_points: bool,
    max_lag_days: int | None,
) -> float | None:
    latest_date, latest_value = points[-1]
    target_date = latest_date - timedelta(days=days)
    candidates = [
        point
        for point in points[:-1]
        if point[0] <= target_date and (max_lag_days is None or point[0] >= target_date - timedelta(days=max_lag_days))
    ]
    if not candidates:
        return None
    return _normalized_change(latest_value, candidates[-1][1], basis_points=basis_points)


def _period_change(
    points: list[tuple[date, float]],
    *,
    periods: int,
    basis_points: bool,
    months_per_period: int,
    annualization_periods: int | None = None,
) -> float | None:
    if not points:
        return None
    latest_date, latest_value = points[-1]
    target_month = _shift_month(latest_date, -(periods * months_per_period))
    prior_values = {(observed_on.year, observed_on.month): value for observed_on, value in points[:-1]}
    prior_value = prior_values.get((target_month.year, target_month.month))
    if prior_value is None:
        return None
    if basis_points:
        return round((latest_value - prior_value) * 100, 4)
    if prior_value == 0:
        return None
    ratio = latest_value / prior_value
    if annualization_periods is not None:
        if ratio <= 0:
            return None
        return round((ratio**annualization_periods - 1) * 100, 4)
    return round((ratio - 1) * 100, 4)


def _shift_month(value: date, month_delta: int) -> date:
    absolute_month = value.year * 12 + value.month - 1 + month_delta
    return date(absolute_month // 12, absolute_month % 12 + 1, 1)


def _normalized_change(latest: float, prior: float, *, basis_points: bool) -> float | None:
    if basis_points:
        return round((latest - prior) * 100, 4)
    if prior == 0:
        return None
    return round((latest / prior - 1) * 100, 4)


def _release_delta(row: dict[str, Any]) -> dict[str, float | None]:
    actual = float(row["actual_value"]) if row.get("actual_value") is not None else None
    estimate = float(row["estimate_value"]) if row.get("estimate_value") is not None else None
    prior = float(row["prior_value"]) if row.get("prior_value") is not None else None
    revised = float(row["revised_prior_value"]) if row.get("revised_prior_value") is not None else None
    return {
        "surprise": round(actual - estimate, 6) if actual is not None and estimate is not None else None,
        "revision": round(revised - prior, 6) if revised is not None and prior is not None else None,
    }


def _latest_fact_summaries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity = ":".join(
            filter(
                None,
                (
                    str(row["dataset_id"]),
                    str(row.get("series_id") or ""),
                    str(row.get("contract_code") or ""),
                ),
            )
        )
        if identity not in latest or _fact_order(row) > _fact_order(latest[identity]):
            latest[identity] = row
    return [
        {
            "dataset_id": row["dataset_id"],
            "series_id": row.get("series_id"),
            "contract_code": row.get("contract_code"),
            "fact_ref": row.get("fact_id")
            or row.get("observation_id")
            or row.get("position_fact_id")
            or row.get("settlement_id")
            or row.get("release_fact_id")
            or row.get("analysis_id")
            or row.get("role_fact_id")
            or row.get("document_id"),
            "reference": _fact_reference(row),
            "value": row.get("title") if row.get("document_id") else _fact_value(row),
            "unit": row.get("unit") or ("document" if row.get("document_id") else "unknown"),
            "observed_at_ms": (int(row["observed_at_ms"]) if row.get("observed_at_ms") is not None else None),
            "published_at_ms": row.get("published_at_ms"),
            "received_at_ms": int(row["received_at_ms"]),
            "source_url": row["source_url"],
        }
        for row in sorted(
            latest.values(),
            key=lambda item: (
                str(item["dataset_id"]),
                str(item.get("series_id") or ""),
                str(item.get("contract_code") or ""),
                _fact_reference(item) or "",
                str(
                    item.get("fact_id")
                    or item.get("observation_id")
                    or item.get("position_fact_id")
                    or item.get("settlement_id")
                    or item.get("release_fact_id")
                    or ""
                ),
            ),
        )
    ]


def _reconciliation_receipts(
    specs: tuple[DatasetSpec, ...],
    facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    specs_by_concept: dict[str, list[DatasetSpec]] = defaultdict(list)
    for spec in specs:
        specs_by_concept[spec.concept_id].append(spec)
    latest_by_dataset: dict[str, dict[str, Any]] = {}
    for row in facts:
        dataset_id = str(row["dataset_id"])
        if dataset_id not in latest_by_dataset or _fact_order(row) > _fact_order(latest_by_dataset[dataset_id]):
            latest_by_dataset[dataset_id] = row

    receipts = []
    for concept_id, concept_specs in sorted(specs_by_concept.items()):
        if len(concept_specs) < 2:
            continue
        if any(spec.metadata.get("tenors") for spec in concept_specs):
            continue
        observations = []
        for spec in sorted(concept_specs, key=lambda item: (item.source_role, item.dataset_id)):
            fact = latest_by_dataset.get(spec.dataset_id)
            observations.append(
                {
                    "dataset_id": spec.dataset_id,
                    "source_role": spec.source_role,
                    "reference": _fact_reference(fact),
                    "value": _fact_value(fact) if fact is not None else None,
                    "unit": spec.unit,
                    "fact_ref": (
                        fact.get("fact_id")
                        or fact.get("observation_id")
                        or fact.get("release_fact_id")
                        or fact.get("settlement_id")
                        if fact is not None
                        else None
                    ),
                }
            )
        primary = next(
            (
                item
                for item in observations
                if item["source_role"] in {"decision_primary", "release"} and item["fact_ref"] is not None
            ),
            None,
        )
        comparisons = []
        if primary is not None and primary["value"] is not None:
            for observation in observations:
                if observation is primary or observation["value"] is None or observation["unit"] != primary["unit"]:
                    continue
                tolerance = _reconciliation_tolerance(str(primary["unit"]))
                difference = round(
                    float(primary["value"]) - float(observation["value"]),
                    6,
                )
                references_aligned = primary["reference"] == observation["reference"]
                comparisons.append(
                    {
                        "left_dataset_id": primary["dataset_id"],
                        "right_dataset_id": observation["dataset_id"],
                        "left_fact_ref": primary["fact_ref"],
                        "right_fact_ref": observation["fact_ref"],
                        "aligned_reference": (primary["reference"] if references_aligned else None),
                        "left_reference": primary["reference"],
                        "right_reference": observation["reference"],
                        "left_value": primary["value"],
                        "right_value": observation["value"],
                        "difference": difference,
                        "tolerance": tolerance,
                        "unit": primary["unit"],
                        "status": (
                            "reference_mismatch"
                            if not references_aligned
                            else "within_tolerance"
                            if abs(difference) <= tolerance
                            else "divergent"
                        ),
                    }
                )
        present = sum(item["fact_ref"] is not None for item in observations)
        receipts.append(
            {
                "concept_id": concept_id,
                "state": "complete" if present == len(observations) else "partial" if present else "insufficient",
                "selection_policy": "decision_primary_only_no_fallback",
                "selected_dataset_id": primary["dataset_id"] if primary is not None else None,
                "identity_policy": "separate_source_facts_no_blend",
                "observations": observations,
                "comparisons": comparisons,
            }
        )
    return receipts


def _reconciliation_tolerance(unit: str) -> float:
    return {
        "percent": 0.05,
        "index": 0.1,
        "price": 0.5,
        "usd_per_barrel": 0.5,
        "usdt": 50.0,
    }.get(unit, 0.0)


def _next_checkpoints(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checkpoints = []
    for state in states:
        if state["current_health"] != "current":
            reason = state["current_reason"]
        elif state["history_depth"] in {"partial", "insufficient"}:
            reason = state["history_reason"]
        elif state["critical"]:
            reason = state["current_reason"]
            if not isinstance(reason, Mapping) or reason.get("next_check_at_ms") is None:
                continue
        else:
            continue
        if not isinstance(reason, Mapping):
            continue
        checkpoints.append(
            {
                "dataset_id": state["dataset_id"],
                "label": state["label"],
                "current_health": state["current_health"],
                "history_depth": state["history_depth"],
                "reason": reason,
                "next_check_at_ms": reason.get("next_check_at_ms"),
            }
        )
    return checkpoints[:12]


def _summary_headline(changes: list[dict[str, Any]]) -> str | None:
    if not changes:
        return None
    change = changes[0]
    primary_change = change.get("primary_change")
    if isinstance(primary_change, int | float):
        value = _reader_value(float(primary_change), str(change["metric_unit"]), signed=True)
    else:
        value = _reader_value(float(change["value"]), str(change["unit"]), signed=False)
    as_of = f"，截至 {change['as_of']}" if change.get("as_of") else ""
    return f"{change['label']}：{value}{as_of}"


def _reader_value(value: float, unit: str, *, signed: bool) -> str:
    rendered = f"{value:+.4g}" if signed else f"{value:.6g}"
    return f"{rendered}{_reader_unit(unit)}"


def _reader_unit(unit: str) -> str:
    return {
        "basis_points": "bp",
        "billions_chained_2017_usd": "十亿 2017 年不变价美元",
        "billions_usd": "十亿美元",
        "bp": "bp",
        "index": "点",
        "index_points": "点",
        "millions_usd": "百万美元",
        "percent": "%",
        "percent_open_interest": "% OI",
        "persons": "人",
        "price": "",
        "thousands_persons": "千人",
        "usd_per_barrel": "美元/桶",
        "usdt": " USDT",
    }.get(unit, "（单位未解释）")


def _change_metric_label(metric: str) -> str:
    return {
        "change_1d_bp": "1日变化",
        "change_1m_bp": "1月变化",
        "change_1w_bp": "1周变化",
        "change_3m_bp": "3个月变化",
        "change_4w_bp": "4周变化",
        "change_4w_pct": "4周变化",
        "change_wow_bp": "周环比",
        "change_wow_pct": "周环比",
        "mom_bp": "月环比",
        "mom_pct": "月环比",
        "qoq_annualized_pct": "季环比年化",
        "qoq_bp": "季环比",
        "return_1d_pct": "1日回报",
        "return_1m_pct": "1月回报",
        "return_1w_pct": "1周回报",
        "revision": "前值修订",
        "surprise": "相对预期",
        "three_month_annualized_pct": "3个月年化",
        "yoy_bp": "同比",
        "yoy_pct": "同比",
    }.get(metric, "登记指标变化")


def _trust_tier_label(trust_tier: str) -> str:
    return {
        "exchange": "交易所来源",
        "official": "官方来源",
        "untrusted_proxy": "代理来源，需谨慎核对",
    }.get(trust_tier, "来源等级未标注")


def _contradictions(_module_id: MacroModuleId, changes: list[dict[str, Any]]) -> list[str]:
    rising = [
        str(item["label"])
        for item in changes
        if isinstance(item.get("primary_change"), int | float) and float(item["primary_change"]) > 0
    ]
    falling = [
        str(item["label"])
        for item in changes
        if isinstance(item.get("primary_change"), int | float) and float(item["primary_change"]) < 0
    ]
    if rising and falling:
        return [f"{'、'.join(rising[:2])}上行；{'、'.join(falling[:2])}下行，短窗口方向分化。"]
    return []


def _optional_difference(left: float | None, right: float | None, *, multiplier: float = 1) -> float | None:
    return round((left - right) * multiplier, 4) if left is not None and right is not None else None


def _fact_value(row: dict[str, Any]) -> float | None:
    for key in ("value_numeric", "leveraged_net_pct_oi", "settlement_price", "actual_value"):
        if row.get(key) is not None:
            return float(row[key])
    return None


def _minimum_timestamp(*values: object) -> int | None:
    timestamps = [int(value) for value in values if isinstance(value, int)]
    return min(timestamps) if timestamps else None


def _fact_reference(row: dict[str, Any] | None) -> str | None:
    if row is None:
        return None
    for key in ("reference_date", "trade_date", "report_date", "reference_period", "effective_date"):
        if row.get(key) is not None:
            return str(row[key])
    observed_at_ms = row.get("observed_at_ms")
    if observed_at_ms is not None:
        return datetime.fromtimestamp(int(observed_at_ms) / 1_000, tz=UTC).date().isoformat()
    return None


def _fact_date(row: dict[str, Any]) -> date | None:
    for key in ("reference_date", "trade_date", "report_date", "effective_date"):
        value = row.get(key)
        if isinstance(value, date):
            return value
        if value:
            try:
                return date.fromisoformat(str(value)[:10])
            except ValueError:
                pass
    if row.get("observed_at_ms") is not None:
        return datetime.fromtimestamp(int(row["observed_at_ms"]) / 1_000, tz=UTC).date()
    reference_period = str(row.get("reference_period") or "")
    if reference_period:
        normalized = reference_period.replace("-M", "-")
        parts = normalized.split("-")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            try:
                return date(int(parts[0]), int(parts[1]), 1)
            except ValueError:
                return None
    return None


def _history_start_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if value:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    return None


def _fact_clock_ms(row: dict[str, Any]) -> int:
    if row.get("observed_at_ms") is not None:
        return int(row["observed_at_ms"])
    if row.get("published_at_ms") is not None:
        return int(row["published_at_ms"])
    return int(row["received_at_ms"])


def _freshness_age_ms(
    spec: DatasetSpec,
    row: dict[str, Any],
    now_ms: int,
    *,
    expected_at_ms: int | None = None,
) -> int:
    reference = _fact_date(row)
    if spec.frequency != "daily" or reference is None:
        return max(0, int(expected_at_ms if expected_at_ms is not None else now_ms) - _fact_clock_ms(row))
    current_date = datetime.fromtimestamp(now_ms / 1_000, tz=UTC).astimezone(_NEW_YORK).date()
    completed_weekdays = 0
    candidate = reference + timedelta(days=1)
    while candidate < current_date:
        if candidate.weekday() < 5:
            completed_weekdays += 1
        candidate += timedelta(days=1)
    return completed_weekdays * _DAY_MS


def _fact_order(row: dict[str, Any]) -> tuple[str, int, int]:
    return (
        _fact_reference(row) or "",
        _fact_clock_ms(row),
        int(row["received_at_ms"]),
    )


def _release_order(row: dict[str, Any]) -> tuple[str, int, int, int, str]:
    revision_rank = int(row.get("revised_prior_value") is not None)
    return (
        str(row.get("reference_period") or ""),
        revision_rank,
        int(row.get("published_at_ms") or -1),
        int(row.get("received_at_ms") or 0),
        str(row.get("fact_hash") or row.get("release_fact_id") or ""),
    )


__all__ = ["build_typed_module_payload", "schema_version_for_module"]
