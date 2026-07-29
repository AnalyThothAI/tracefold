from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

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
from tracefold.macro.registry import DATASET_REGISTRY, datasets_for_module

_SCHEMA_VERSIONS: dict[MacroModuleId, str] = {
    "rates_fed": "macro_rates_fed_v4",
    "economy_inflation": "macro_economy_inflation_v4",
    "liquidity_funding": "macro_liquidity_funding_v4",
    "credit": "macro_credit_v5",
    "volatility": "macro_volatility_v4",
    "cross_asset": "macro_cross_asset_v5",
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
    changes = _top_changes(specs, series_rows, market_rows, settlement_rows, release_rows)
    payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSIONS[module_id],
        "module_id": module_id,
        "label": MACRO_MODULE_LABELS[module_id],
        "status": {
            "coverage": coverage,
            "current_health": current_health,
            "history_depth": history_depth,
        },
        "latest_fact_at_ms": latest_fact_at_ms,
        "summary": {
            "headline": f"{MACRO_MODULE_LABELS[module_id]}当前证据",
            "interpretation": "实时模块只呈现可复算事实；主线判断由冻结 Macro Thesis 发布。",
            "top_changes": changes[:6],
        },
        "contradictions": _contradictions(module_id, changes),
        "falsifiers": _falsifiers(module_id),
        "next_checkpoints": _next_checkpoints(dataset_states),
        "evidence": {
            "dataset_states": dataset_states,
            "latest_facts": _latest_fact_summaries(module_facts),
            "reconciliation_receipts": _reconciliation_receipts(specs, module_facts),
        },
    }
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
                "reason": None if active else "dataset_not_registered",
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
        states.append(
            {
                "dataset_id": spec.dataset_id,
                "concept_id": spec.concept_id,
                "source_role": spec.source_role,
                "label": spec.label,
                "current_health": current_health,
                "history_depth": history_state,
                "market_state": market.state if market is not None else "not_applicable",
                "source_state": source_state,
                "current_reason": current_reason,
                "history_reason": history_reason,
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
    if status in {"unavailable", "invalid"}:
        return "failed"
    return "degraded"


def _dataset_history_depth(
    spec: DatasetSpec,
    targets: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    *,
    active_backfills: list[dict[str, Any]],
) -> tuple[str, str]:
    backfills = [row for row in targets if str(row.get("clock_kind")) == "backfill"]
    history_required = bool(backfills) or spec.source_role == "history" or bool(spec.metadata.get("history_years"))
    if not history_required:
        return "not_required", "no_history_requirement"

    dated_facts = sorted(value for row in facts if (value := _fact_date(row)) is not None)
    if backfills and all(str(row.get("status")) == "current" for row in backfills):
        return "complete", "configured_history_range_complete"
    if active_backfills:
        return (
            ("partial", "history_backfill_in_progress")
            if dated_facts
            else ("insufficient", "history_backfill_has_no_valid_fact")
        )
    if not dated_facts:
        return "insufficient", "history_facts_missing"

    expected_years = int(spec.metadata.get("history_years") or 5)
    covered_days = (dated_facts[-1] - dated_facts[0]).days
    if covered_days >= expected_years * 365 - 14:
        return "complete", "expected_history_window_present"
    return "partial", "expected_history_window_incomplete"


def _current_health(dataset_states: list[dict[str, Any]]) -> dict[str, Any]:
    tracked = [item for item in dataset_states if item["source_role"] != "history"]
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
    tracked = [state for state in dataset_states if state["history_depth"] != "not_required"]
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
    return {
        "curve": calculate_curve_contract(series_rows),
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
            "etfs": _combined_asset_rows(
                groups["market_rows"],
                (
                    ("nasdaq.lqd.daily", "yfinance.lqd.intraday"),
                    ("nasdaq.hyg.daily", "yfinance.hyg.intraday"),
                ),
            ),
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
    return {
        "term_structure": {
            "spot_and_three_month": _indicator_rows(series_rows, ("fred.vixcls", "fred.vxvcls")),
            "spread_history": _paired_difference_history(vix, vxv),
        },
        "cross_asset_implied": {
            "indicators": _indicator_rows(
                series_rows,
                cross_asset_ids,
            ),
            "normalized": _normalized_indicator_history(
                series_rows,
                cross_asset_ids,
                {
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
    proxy_rows = _combined_asset_rows(market_rows, _ETF_DATASETS)
    proxy_by_dataset = {row["history_dataset_id"]: row for row in proxy_rows}
    bitcoin_rows = _combined_asset_rows(
        market_rows,
        (("binance.btcusdt.spot", "yfinance.btc_yahoo.intraday"),),
    )
    vix_intraday = _asset_rows(market_rows, ("yfinance.vix_index.intraday",))
    wti = _indicator_rows(series_rows, ("fred.dcoilwtico",))
    vix_daily = _indicator_rows(series_rows, ("fred.vixcls",))
    vix_row = _reconciled_asset_sources(
        decision_primary=vix_daily[0] if vix_daily else None,
        intraday_proxy=vix_intraday[0] if vix_intraday else None,
    )
    benchmarks = [
        _benchmark("S&P 500", "equity", "nasdaq.spy.daily", proxy_by_dataset.get("nasdaq.spy.daily")),
        _benchmark("Nasdaq 100", "equity", "nasdaq.qqq.daily", proxy_by_dataset.get("nasdaq.qqq.daily")),
        _benchmark("Russell 2000", "equity", "nasdaq.iwm.daily", proxy_by_dataset.get("nasdaq.iwm.daily")),
        _benchmark("Treasury duration", "rates", "nasdaq.tlt.daily", proxy_by_dataset.get("nasdaq.tlt.daily")),
        _benchmark("IG credit", "credit", "nasdaq.lqd.daily", proxy_by_dataset.get("nasdaq.lqd.daily")),
        _benchmark("HY credit", "credit", "nasdaq.hyg.daily", proxy_by_dataset.get("nasdaq.hyg.daily")),
        _benchmark("U.S. dollar", "fx", "nasdaq.dxy.daily", proxy_by_dataset.get("nasdaq.dxy.daily")),
        _benchmark("Gold", "commodity", "nasdaq.gld.daily", proxy_by_dataset.get("nasdaq.gld.daily")),
        _benchmark("WTI Cushing spot", "commodity", "fred.dcoilwtico", wti[0] if wti else None, official=True),
        _benchmark(
            "Bitcoin",
            "crypto",
            "binance.btcusdt.spot",
            bitcoin_rows[0] if bitcoin_rows else None,
            official=True,
        ),
        _benchmark("Intermediate Treasury", "rates", "nasdaq.ief.daily", proxy_by_dataset.get("nasdaq.ief.daily")),
        _benchmark(
            "VIX",
            "volatility",
            "fred.vixcls",
            vix_row,
            official=True,
        ),
    ]
    return {
        "assets": {
            "benchmarks": benchmarks,
            "proxies": proxy_rows,
            "normalized": comparison["normalized"],
        },
        "correlations": comparison["correlations"],
        "futures": {
            "market": _combined_asset_rows(market_rows, _FUTURES_DATASETS),
            "vix_settlements": _settlement_rows(groups["settlement_rows"], "cboe.cfe.vx.settlement"),
            "positions": _position_rows(groups["position_rows"], "cftc.tff.cross_asset_positions"),
        },
    }


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
            "reason": (
                f"analysis:{institutional['analysis']['analysis_id']}"
                if institutional
                else "immutable_document_analysis_not_published"
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


def _asset_rows(
    market_rows: list[dict[str, Any]],
    dataset_ids: tuple[str, ...],
) -> list[dict[str, Any]]:
    calculated = calculate_market_statistics(market_rows, dataset_ids)
    output: list[dict[str, Any]] = []
    for item in calculated:
        dataset_id = str(item["dataset_id"])
        spec = DATASET_REGISTRY[dataset_id]
        output.append(
            {
                **item,
                "dataset_id": dataset_id,
                "symbol": spec.symbol,
                "label": spec.label,
                "instrument_type": spec.instrument_type,
                "asset_class": spec.asset_class,
                "unit": spec.unit,
                "trust_tier": spec.trust_tier,
                "market_time_ms": int(item["market_time_ms"]),
            }
        )
    return output


def _combined_asset_rows(
    market_rows: list[dict[str, Any]],
    dataset_pairs: tuple[tuple[str, str], ...],
) -> list[dict[str, Any]]:
    dataset_ids = tuple(dataset_id for pair in dataset_pairs for dataset_id in pair)
    calculated = {str(item["dataset_id"]): item for item in calculate_market_statistics(market_rows, dataset_ids)}
    output: list[dict[str, Any]] = []
    for history_dataset_id, price_dataset_id in dataset_pairs:
        history = calculated.get(history_dataset_id)
        price = calculated.get(price_dataset_id)
        current = price or history
        if current is None:
            continue
        history_spec = DATASET_REGISTRY[history_dataset_id]
        price_spec = DATASET_REGISTRY[price_dataset_id]
        sources = _reconciled_asset_sources(
            decision_primary=(
                history
                if history_spec.source_role == "decision_primary"
                else price
                if price_spec.source_role == "decision_primary"
                else None
            ),
            intraday_proxy=(
                price
                if price_spec.source_role == "intraday_proxy"
                else history
                if history_spec.source_role == "intraday_proxy"
                else None
            ),
            # The canonical daily Dataset is also the row's explicit history
            # source. Repeating that exact record under the history lens does
            # not transfer identity to the independent intraday proxy.
            history=history,
        )
        output.append(
            {
                "concept_id": history_spec.concept_id,
                "price_dataset_id": price_dataset_id,
                "history_dataset_id": history_dataset_id,
                "symbol": price_spec.symbol or history_spec.symbol,
                "label": price_spec.label,
                "instrument_type": price_spec.instrument_type,
                "asset_class": price_spec.asset_class,
                "selection_policy": (
                    "decision_primary_only_no_fallback"
                    if sources["decision_primary"] is not None
                    else "intraday_proxy_for_current_history_separate"
                ),
                "sources": sources,
            }
        )
    return output


def _reconciled_asset_sources(
    *,
    decision_primary: dict[str, Any] | None = None,
    intraday_proxy: dict[str, Any] | None = None,
    history: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "identity_policy": "separate_source_facts_no_blend",
        "decision_primary": decision_primary,
        "intraday_proxy": intraday_proxy,
        "history": history,
    }


def _benchmark(
    label: str,
    asset_class: str,
    dataset_id: str,
    row: dict[str, Any] | None,
    *,
    official: bool = False,
) -> dict[str, Any]:
    registry_symbol = DATASET_REGISTRY[dataset_id].symbol
    sources = (
        row["sources"]
        if row is not None and isinstance(row.get("sources"), dict)
        else row
        if row is not None and row.get("identity_policy") == "separate_source_facts_no_blend"
        else _reconciled_asset_sources(
            decision_primary=row if official else None,
            history=row if not official else None,
        )
    )
    return {
        "label": label,
        "symbol": (str(row["symbol"]) if row is not None and row.get("symbol") else str(registry_symbol or label)),
        "asset_class": asset_class,
        "dataset_id": dataset_id,
        "evidence_kind": "official_benchmark" if official else "tradable_proxy_reference",
        "selection_policy": "decision_primary_only_no_fallback" if official else "proxy_current_history_separate",
        "sources": sources,
    }


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
    matching = sorted(
        (row for row in rows if row["dataset_id"] == dataset_id),
        key=lambda row: (row["trade_date"], str(row["contract_code"])),
        reverse=True,
    )
    return [
        {
            "trade_date": str(row["trade_date"]),
            "contract_code": row["contract_code"],
            "settlement_price": float(row["settlement_price"]),
            "open_interest": row.get("open_interest"),
            "volume": row.get("volume"),
            "source_url": row["source_url"],
        }
        for row in matching[:30]
    ]


def _current_settlement_revisions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    revisions: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
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
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        dataset_id = str(row["dataset_id"])
        if dataset_id not in dataset_ids:
            continue
        if dataset_id not in latest or _release_order(row) > _release_order(latest[dataset_id]):
            latest[dataset_id] = row
    return [
        {
            "dataset_id": dataset_id,
            "label": DATASET_REGISTRY[dataset_id].label,
            "reference_period": row["reference_period"],
            "scheduled_at_ms": (int(row["scheduled_at_ms"]) if row.get("scheduled_at_ms") is not None else None),
            "actual_value": row["actual_value"],
            "estimate_value": row["estimate_value"],
            "prior_value": row["prior_value"],
            "revised_prior_value": row["revised_prior_value"],
            **_release_delta(row),
            "unit": row["unit"],
            "published_at_ms": int(row["published_at_ms"]) if row.get("published_at_ms") is not None else None,
            "received_at_ms": int(row["received_at_ms"]),
            "source_url": row["source_url"],
        }
        for dataset_id, row in sorted(latest.items())
    ]


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
                        f"{strongest_name}={strongest_value:g} {metric_unit}; "
                        f"标准化幅度={standardized_magnitude:g}; "
                        f"决策相关性={decision_relevance}; 来源={spec.trust_tier}"
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
            "published_at_ms": row.get("published_at_ms"),
            "received_at_ms": int(row["received_at_ms"]),
            "source_url": row["source_url"],
        }
        for row in sorted(latest.values(), key=lambda item: (str(item["dataset_id"]), _fact_reference(item) or ""))
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
    return [
        {
            "dataset_id": state["dataset_id"],
            "label": state["label"],
            "current_health": state["current_health"],
            "history_depth": state["history_depth"],
            "next_check": "按 Dataset Registry 数据时钟自动检查",
        }
        for state in states
        if state["critical"]
        or state["current_health"] != "current"
        or state["history_depth"] in {"partial", "insufficient"}
    ][:12]


def _contradictions(module_id: MacroModuleId, changes: list[dict[str, Any]]) -> list[str]:
    signs = {float(item["primary_change"]) > 0 for item in changes[:6] if item["primary_change"] is not None}
    if len(signs) > 1:
        return [f"{MACRO_MODULE_LABELS[module_id]}的主要分项短窗口方向不一致，不能压成单一叙事。"]
    return []


def _falsifiers(module_id: MacroModuleId) -> list[str]:
    return {
        "rates_fed": ["2Y、10Y 与政策走廊的相对变化反转当前曲线分类。"],
        "economy_inflation": ["连续两次核心通胀或就业发布改变当前方向。"],
        "liquidity_funding": ["SOFR越过政策走廊或准备金连续显著下降。"],
        "credit": ["评级梯级尾部、贷款供给或逾期核销同时恶化。"],
        "volatility": ["VIX期限结构由升水切换为持续倒挂。"],
        "cross_asset": ["固定资产矩阵的1周与1月方向同时反转。"],
    }[module_id]


def _optional_difference(left: float | None, right: float | None, *, multiplier: float = 1) -> float | None:
    return round((left - right) * multiplier, 4) if left is not None and right is not None else None


def _fact_value(row: dict[str, Any]) -> float | None:
    for key in ("value_numeric", "leveraged_net_pct_oi", "settlement_price", "actual_value"):
        if row.get(key) is not None:
            return float(row[key])
    return None


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


def _fact_clock_ms(row: dict[str, Any]) -> int:
    if row.get("observed_at_ms") is not None:
        return int(row["observed_at_ms"])
    if row.get("published_at_ms") is not None:
        return int(row["published_at_ms"])
    reference_date = _fact_date(row)
    if reference_date is not None:
        value = datetime(reference_date.year, reference_date.month, reference_date.day, 21, tzinfo=UTC)
        return int(value.timestamp() * 1_000)
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
