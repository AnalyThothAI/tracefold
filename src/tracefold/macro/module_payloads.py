from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from tracefold.macro.calculations import (
    calculate_curve_contract,
    calculate_funding_cost_comparisons,
    calculate_market_comparison,
    calculate_market_statistics,
    calculate_series_statistics,
)
from tracefold.macro.coverage import coverage_for_module
from tracefold.macro.domain import MACRO_MODULE_LABELS, DatasetSpec, MacroModuleId
from tracefold.macro.fed_roles import match_effective_role
from tracefold.macro.registry import DATASET_REGISTRY, datasets_for_module

_SCHEMA_VERSIONS: dict[MacroModuleId, str] = {
    "rates_fed": "macro_rates_fed_v2",
    "economy_inflation": "macro_economy_inflation_v2",
    "liquidity_funding": "macro_liquidity_funding_v2",
    "credit": "macro_credit_v2",
    "volatility": "macro_volatility_v2",
    "cross_asset": "macro_cross_asset_v2",
}
_HEALTH_ORDER = {
    "invalid": 6,
    "unavailable": 5,
    "stale": 4,
    "backfilling": 3,
    "delayed": 2,
    "current": 1,
}
_ETF_IDS = (
    "nasdaq.spy.history",
    "nasdaq.qqq.history",
    "nasdaq.iwm.history",
    "nasdaq.tlt.history",
    "nasdaq.ief.history",
    "nasdaq.lqd.history",
    "nasdaq.hyg.history",
    "nasdaq.dxy.history",
    "nasdaq.gld.history",
    "nasdaq.uso.history",
)
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
    latest_fact_at_ms = max((int(row["received_at_ms"]) for row in module_facts), default=0)
    dataset_states = _dataset_states(
        specs,
        target_states,
        module_facts,
        now_ms,
        analysis_job_state=analysis_job_state,
    )
    coverage = _coverage(module_id)
    data_health = _data_health(dataset_states)
    critical_blocked = any(
        (state["critical"] and state["state"] in {"invalid", "unavailable", "backfilling", "stale"})
        or state["reason"] == "required_history_backfill_incomplete"
        for state in dataset_states
    )
    changes = _top_changes(specs, series_rows, market_rows, settlement_rows)
    payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSIONS[module_id],
        "module_id": module_id,
        "label": MACRO_MODULE_LABELS[module_id],
        "status": {
            "coverage": coverage,
            "data_health": data_health,
            "judgment": {
                "state": "blocked" if coverage["state"] == "partial" or critical_blocked else "missing",
                "cutoff_ms": None,
            },
        },
        "latest_fact_at_ms": latest_fact_at_ms,
        "summary": {
            "headline": f"{MACRO_MODULE_LABELS[module_id]}当前证据",
            "interpretation": "实时页面只呈现可复算事实；判断只由冻结 Evidence Pack 发布。",
            "top_changes": changes[:6],
        },
        "contradictions": _contradictions(module_id, changes),
        "falsifiers": _falsifiers(module_id),
        "next_checkpoints": _next_checkpoints(dataset_states),
        "evidence": {
            "dataset_states": dataset_states,
            "latest_facts": _latest_fact_summaries(module_facts),
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
    licensed_missing = False
    for capability in coverage_for_module(module_id):
        datasets = [DATASET_REGISTRY.get(dataset_id) for dataset_id in capability.dataset_ids]
        active = all(spec is not None and spec.adapter_id != "unavailable" for spec in datasets)
        if capability.requirement == "licensed_unavailable":
            state = "licensed_unavailable"
            licensed_missing = True
        elif active:
            state = "available"
        else:
            state = "missing"
            if capability.requirement == "required":
                required_missing = True
        capabilities.append(
            {
                "capability_id": capability.capability_id,
                "label": capability.label,
                "requirement": capability.requirement,
                "state": state,
                "dataset_ids": list(capability.dataset_ids),
                "reason": capability.unavailable_reason
                or (
                    next(
                        (
                            str(spec.metadata.get("unavailable_reason"))
                            for spec in datasets
                            if spec is not None and spec.adapter_id == "unavailable"
                        ),
                        None,
                    )
                    if not active
                    else None
                ),
            }
        )
    state = "partial" if required_missing else "licensed_unavailable" if licensed_missing else "complete"
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
            if str(row.get("clock_kind")) == "backfill" and str(row.get("status")) != "current"
        ]
        latest = latest_by_dataset.get(spec.dataset_id)
        if spec.adapter_id == "unavailable":
            state = "unavailable"
            reason = str(spec.metadata.get("unavailable_reason") or "source_not_configured")
        elif (
            spec.dataset_id == "federal_reserve.document.analysis"
            and analysis_job_state is not None
            and int(analysis_job_state.get("failed", 0)) > 0
        ):
            state = "invalid"
            reason = "document_analysis_jobs_failed"
        elif (
            spec.dataset_id == "federal_reserve.document.analysis"
            and analysis_job_state is not None
            and int(analysis_job_state.get("open", 0)) > 0
        ):
            state = "backfilling"
            reason = "document_analysis_jobs_open"
        elif spec.adapter_id.startswith("derived_") and latest is None:
            state = "backfilling"
            reason = "derived_fact_pending"
        elif spec.adapter_id.startswith("derived_"):
            if latest is None:
                raise RuntimeError("derived_macro_fact_missing")
            age_ms = _freshness_age_ms(spec, latest, now_ms)
            if age_ms > spec.freshness_seconds * 2_000:
                state, reason = "stale", "derived_fact_past_freshness_budget"
            elif age_ms > spec.freshness_seconds * 1_000:
                state, reason = "delayed", "derived_fact_delayed"
            else:
                state, reason = "current", "within_freshness_budget"
        elif active_backfills:
            state = max(
                (
                    _target_health_state(
                        str(row.get("status") or ""),
                        backfill=True,
                    )
                    for row in active_backfills
                ),
                key=lambda item: _HEALTH_ORDER[item],
            )
            reason = "required_history_backfill_incomplete"
        elif target is None:
            state = "invalid"
            reason = "acquisition_target_missing"
        elif latest is None:
            state = _target_health_state(
                str(target["status"]),
                backfill=False,
            )
            reason = "no_valid_fact"
        else:
            age_ms = _freshness_age_ms(spec, latest, now_ms)
            if age_ms > spec.freshness_seconds * 2_000:
                state, reason = "stale", "fact_past_freshness_budget"
            elif age_ms > spec.freshness_seconds * 1_000:
                state, reason = "delayed", "fact_delayed"
            else:
                state, reason = "current", "within_freshness_budget"
        states.append(
            {
                "dataset_id": spec.dataset_id,
                "label": spec.label,
                "state": state,
                "reason": reason,
                "critical": spec.critical,
                "trust_tier": spec.trust_tier,
                "source_url": spec.source_url,
                "latest_reference": _fact_reference(latest),
                "latest_received_at_ms": int(latest["received_at_ms"]) if latest else None,
            }
        )
    return states


def _target_health_state(status: str, *, backfill: bool) -> str:
    if status == "current":
        return "current"
    if status in {"pending", "claimed", "backfilling"}:
        return "backfilling" if backfill or status in {"pending", "backfilling"} else "delayed"
    if status in _HEALTH_ORDER:
        return status
    return "invalid"


def _data_health(dataset_states: list[dict[str, Any]]) -> dict[str, Any]:
    active = [
        state
        for state in dataset_states
        if state["reason"]
        not in {
            "ice_bofa_history_before_public_three_year_window_unavailable",
            "licensed_contract_facts_not_configured",
            "licensed_security_level_facts_not_configured",
        }
    ]
    state = max((str(item["state"]) for item in active), key=lambda item: _HEALTH_ORDER.get(item, 0), default="current")
    return {
        "state": state,
        "current_datasets": sum(item["state"] == "current" for item in active),
        "tracked_datasets": len(active),
        "as_of_ms": max(
            (int(item["latest_received_at_ms"]) for item in active if item["latest_received_at_ms"] is not None),
            default=0,
        ),
    }


def _rates_payload(**groups: list[dict[str, Any]]) -> dict[str, Any]:
    series_rows = groups["series_rows"]
    return {
        "curve": calculate_curve_contract(series_rows),
        "policy_pricing": {
            "rates": _indicator_rows(
                series_rows,
                ("fred.effr", "fred.dfedtaru", "fred.dfedtarl", "fred.sofr", "fred.dgs2", "fred.dgs10"),
            ),
            "cme_policy_probabilities": {
                "state": "licensed_unavailable",
                "reason": "licensed_contract_facts_not_configured",
            },
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
                ("bls.cpi.release", "bls.core_cpi.release"),
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
        },
    }


def _liquidity_payload(**groups: list[dict[str, Any]]) -> dict[str, Any]:
    series_rows = groups["series_rows"]
    return {
        "balance_sheet": {
            "indicators": _indicator_rows(
                series_rows,
                ("fred.walcl", "fred.wrbwfrbl", "fred.wtregen", "fred.rrpontsyd"),
            ),
        },
        "funding": {
            "indicators": _indicator_rows(series_rows, ("fred.sofr", "fred.iorb")),
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
            "etfs": _asset_rows(
                groups["market_rows"],
                ("nasdaq.lqd.history", "nasdaq.hyg.history"),
            ),
            "positions": _position_rows(groups["position_rows"], "cftc.tff.credit_positions"),
            "trace_nav": {
                "state": "licensed_unavailable",
                "reason": "licensed_security_level_facts_not_configured",
            },
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
        _credit_dimension(
            "market_liquidity",
            "市场流动性",
            "licensed_unavailable",
            "TRACE逐笔与ETF NAV溢折价在获得合规数据前不可用",
            ["licensed.credit.trace_nav"],
            [],
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
    return {
        "term_structure": {
            "spot_and_three_month": _indicator_rows(series_rows, ("fred.vixcls", "fred.vxvcls")),
            "spread_history": _paired_difference_history(vix, vxv),
        },
        "cross_asset_implied": {
            "indicators": _indicator_rows(
                series_rows,
                ("fred.vxncls", "fred.gvzcls", "fred.ovxcls"),
            ),
        },
    }


def _cross_asset_payload(**groups: list[dict[str, Any]]) -> dict[str, Any]:
    market_rows = groups["market_rows"]
    series_rows = groups["series_rows"]
    symbol_by_dataset = {dataset_id: str(DATASET_REGISTRY[dataset_id].symbol or dataset_id) for dataset_id in _ETF_IDS}
    comparison = calculate_market_comparison(
        market_rows,
        _ETF_IDS,
        symbol_by_dataset,
    )
    proxy_rows = _asset_rows(market_rows, _ETF_IDS)
    proxy_by_dataset = {row["dataset_id"]: row for row in proxy_rows}
    wti = _indicator_rows(series_rows, ("fred.dcoilwtico",))
    vix = _indicator_rows(series_rows, ("fred.vixcls",))
    btc = _asset_rows(market_rows, ("binance.btcusdt.spot",))
    benchmarks = [
        _benchmark("S&P 500", "equity", "nasdaq.spy.history", proxy_by_dataset.get("nasdaq.spy.history")),
        _benchmark("Nasdaq 100", "equity", "nasdaq.qqq.history", proxy_by_dataset.get("nasdaq.qqq.history")),
        _benchmark("Russell 2000", "equity", "nasdaq.iwm.history", proxy_by_dataset.get("nasdaq.iwm.history")),
        _benchmark("Treasury duration", "rates", "nasdaq.tlt.history", proxy_by_dataset.get("nasdaq.tlt.history")),
        _benchmark("IG credit", "credit", "nasdaq.lqd.history", proxy_by_dataset.get("nasdaq.lqd.history")),
        _benchmark("HY credit", "credit", "nasdaq.hyg.history", proxy_by_dataset.get("nasdaq.hyg.history")),
        _benchmark("U.S. dollar", "fx", "nasdaq.dxy.history", proxy_by_dataset.get("nasdaq.dxy.history")),
        _benchmark("Gold", "commodity", "nasdaq.gld.history", proxy_by_dataset.get("nasdaq.gld.history")),
        _benchmark("WTI Cushing spot", "commodity", "fred.dcoilwtico", wti[0] if wti else None, official=True),
        _benchmark("Bitcoin", "crypto", "binance.btcusdt.spot", btc[0] if btc else None, official=True),
        _benchmark("VIX", "volatility", "fred.vixcls", vix[0] if vix else None, official=True),
    ]
    return {
        "assets": {
            "benchmarks": benchmarks,
            "proxies": proxy_rows,
            "normalized": comparison["normalized"],
        },
        "correlations": comparison["correlations"],
        "futures": {
            "vix_settlements": _settlement_rows(groups["settlement_rows"], "cboe.cfe.vx.settlement"),
            "positions": _position_rows(groups["position_rows"], "cftc.tff.cross_asset_positions"),
        },
    }


def _fed_payload(
    document_rows: list[dict[str, Any]],
    role_rows: list[dict[str, Any]],
    analysis_rows: list[dict[str, Any]],
) -> dict[str, Any]:
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
        for role in role_rows:
            if role["effective_start"] > reference_date:
                continue
            if role["effective_end"] is not None and role["effective_end"] < reference_date:
                continue
            official_id = str(role["official_id"])
            current = current_roles.get(official_id)
            if current is None or role["effective_start"] > current["effective_start"]:
                current_roles[official_id] = role
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
            "hawkish": sum(event["analysis"]["stance"] == "hawkish" for event in policy_speeches),
            "neutral": sum(event["analysis"]["stance"] == "neutral" for event in policy_speeches),
            "dovish": sum(event["analysis"]["stance"] == "dovish" for event in policy_speeches),
            "mixed": sum(event["analysis"]["stance"] == "mixed" for event in policy_speeches),
            "not_policy_signal": sum(
                event["analysis"]["policy_relevance"] == "not_policy_signal" for event in speech_analyses
            ),
            "uncertain": sum(event["analysis"]["policy_relevance"] == "uncertain" for event in speech_analyses),
            "analyzed_events": len(speech_analyses),
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
            }
        )
    return output


def _benchmark(
    label: str,
    asset_class: str,
    dataset_id: str,
    row: dict[str, Any] | None,
    *,
    official: bool = False,
) -> dict[str, Any]:
    return {
        "label": label,
        "asset_class": asset_class,
        "dataset_id": dataset_id,
        "evidence_kind": "official_benchmark" if official else "tradable_proxy_reference",
        "latest_value": row.get("latest_value") if row else None,
        "unit": row.get("unit") if row else None,
        "as_of": row.get("as_of") if row else None,
        "change_1w": row.get("change_1w_pct", row.get("change_1w")) if row else None,
        "change_1m": row.get("change_1m_pct", row.get("change_1m")) if row else None,
        "source_url": row.get("source_url") if row else None,
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


def _release_summaries(rows: list[dict[str, Any]], dataset_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        dataset_id = str(row["dataset_id"])
        if dataset_id not in dataset_ids:
            continue
        if dataset_id not in latest or int(row["published_at_ms"]) > int(latest[dataset_id]["published_at_ms"]):
            latest[dataset_id] = row
    return [
        {
            "dataset_id": dataset_id,
            "label": DATASET_REGISTRY[dataset_id].label,
            "reference_period": row["reference_period"],
            "actual_value": row["actual_value"],
            "prior_value": row["prior_value"],
            "revised_prior_value": row["revised_prior_value"],
            "unit": row["unit"],
            "published_at_ms": int(row["published_at_ms"]),
            "source_url": row["source_url"],
        }
        for dataset_id, row in sorted(latest.items())
    ]


def _paired_difference_history(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    left_by_date = {row["reference_date"]: float(row["value_numeric"]) for row in left}
    right_by_date = {row["reference_date"]: float(row["value_numeric"]) for row in right}
    return [
        {
            "date": value.isoformat(),
            "value": round(left_by_date[value] - right_by_date[value], 6),
        }
        for value in sorted(set(left_by_date) & set(right_by_date))
    ][-500:]


def _top_changes(
    specs: tuple[DatasetSpec, ...],
    series_rows: list[dict[str, Any]],
    market_rows: list[dict[str, Any]],
    settlement_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows_by_dataset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in (*series_rows, *market_rows, *settlement_rows):
        rows_by_dataset[str(row["dataset_id"])].append(row)
    changes = []
    for spec in specs:
        rows = sorted(rows_by_dataset.get(spec.dataset_id, []), key=_fact_order)
        if not rows or _fact_value(rows[-1]) is None:
            continue
        latest = float(_fact_value(rows[-1]) or 0)
        short = _absolute_change(rows, 7)
        medium = _absolute_change(rows, 30)
        changes.append(
            {
                "dataset_id": spec.dataset_id,
                "label": spec.label,
                "as_of": _fact_reference(rows[-1]),
                "value": latest,
                "unit": spec.unit,
                "change_1w": short,
                "change_1m": medium,
                "magnitude": max(abs(short or 0), abs(medium or 0)),
                "source_url": rows[-1]["source_url"],
            }
        )
    return sorted(changes, key=lambda item: item["magnitude"], reverse=True)


def _absolute_change(rows: list[dict[str, Any]], days: int) -> float | None:
    latest_date = _fact_date(rows[-1])
    latest_value = _fact_value(rows[-1])
    if latest_date is None or latest_value is None:
        return None
    candidates = [row for row in rows[:-1] if (_fact_date(row) or date.max) <= latest_date - timedelta(days=days)]
    prior_value = _fact_value(candidates[-1]) if candidates else None
    return round(latest_value - prior_value, 6) if prior_value is not None else None


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


def _next_checkpoints(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "dataset_id": state["dataset_id"],
            "label": state["label"],
            "state": state["state"],
            "next_check": "按 Dataset Registry 数据时钟自动检查",
        }
        for state in states
        if state["critical"] or state["state"] != "current"
    ][:12]


def _contradictions(module_id: MacroModuleId, changes: list[dict[str, Any]]) -> list[str]:
    signs = {0 if item["change_1w"] is None else float(item["change_1w"]) > 0 for item in changes[:6]}
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


def _freshness_age_ms(spec: DatasetSpec, row: dict[str, Any], now_ms: int) -> int:
    reference = _fact_date(row)
    if spec.frequency != "daily" or reference is None:
        return max(0, now_ms - _fact_clock_ms(row))
    current_date = datetime.fromtimestamp(now_ms / 1_000, tz=UTC).astimezone(_NEW_YORK).date()
    completed_weekdays = 0
    candidate = reference + timedelta(days=1)
    while candidate < current_date:
        if candidate.weekday() < 5:
            completed_weekdays += 1
        candidate += timedelta(days=1)
    return completed_weekdays * _DAY_MS


def _fact_order(row: dict[str, Any]) -> tuple[str, int]:
    return (_fact_reference(row) or "", int(row["received_at_ms"]))


__all__ = ["build_typed_module_payload", "schema_version_for_module"]
