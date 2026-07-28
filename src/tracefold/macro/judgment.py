from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from typing import Any
from zoneinfo import ZoneInfo

from tracefold.macro.calculations import CALCULATION_REGISTRY, calculate_features
from tracefold.macro.domain import MACRO_MODULE_IDS
from tracefold.macro.module_payloads import build_typed_module_payload
from tracefold.macro.registry import DATASET_REGISTRY
from tracefold.macro.research.completed_session import is_us_market_session

_NEW_YORK = ZoneInfo("America/New_York")
_JUDGMENT_TIME = clock_time(8, 50)
_PACK_SCHEMA = "macro_evidence_pack_v2"
_JUDGMENT_SCHEMA = "macro_daily_judgment_v2"
_COMPILER_VERSION = "macro_professional_coverage_compiler_v3"
_FIXED_ASSETS = {
    "SPY": "yfinance.spy.market",
    "QQQ": "yfinance.qqq.market",
    "IWM": "yfinance.iwm.market",
    "TLT": "yfinance.tlt.market",
    "IEF": "yfinance.ief.market",
    "LQD": "yfinance.lqd.market",
    "HYG": "yfinance.hyg.market",
    "UUP": "yfinance.dxy.market",
    "GLD": "yfinance.gld.market",
    "USO": "yfinance.uso.market",
    "BTC": "yfinance.btc_yahoo.market",
    "VIX": "yfinance.vix_index.market",
}


class MacroJudgmentService:
    """Publish one evidence-sealed, deterministic daily decision record."""

    def __init__(
        self,
        *,
        db: Any,
        settings: Any,
        worker_name: str = "macro_judgment",
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.worker_name = worker_name
        self.clock_ms = clock_ms or _now_ms

    def publish_due(self, *, now_ms: int | None = None) -> dict[str, Any]:
        now = int(now_ms if now_ms is not None else self.clock_ms())
        local_now = datetime.fromtimestamp(now / 1_000, tz=_NEW_YORK)
        session_date = local_now.date()
        if not is_us_market_session(session_date):
            return {"status": "not_due", "reason": "not_us_trading_day"}
        due_at = datetime.combine(session_date, _JUDGMENT_TIME, tzinfo=_NEW_YORK)
        cutoff_ms = int(due_at.timestamp() * 1_000)
        if now < cutoff_ms:
            return {"status": "not_due", "reason": "before_0850_new_york"}

        with self._session() as repos, repos.transaction():
            existing = repos.macro.daily_judgment(session_date)
            if existing is not None:
                repos.macro.upsert_judgment_status(
                    session_date=session_date,
                    judgment_cutoff_ms=cutoff_ms,
                    state="current",
                    reason_code="judgment_published",
                    details={"evidence_pack_id": existing["evidence_pack_id"]},
                    attempted_at_ms=now,
                )
                return {
                    "status": "exists",
                    "session_date": str(session_date),
                    "evidence_pack_id": existing["evidence_pack_id"],
                }

        modules, _features = self._compile_modules(cutoff_ms=cutoff_ms)
        blocked = [module["module_id"] for module in modules if module["status"]["judgment"]["state"] == "blocked"]
        if blocked:
            blocker_details = {
                "blocked_modules": blocked,
                "modules": [
                    {
                        "module_id": module["module_id"],
                        "coverage_gaps": [
                            {
                                "capability_id": capability["capability_id"],
                                "label": capability["label"],
                                "reason": capability["reason"] or capability["state"],
                            }
                            for capability in module["status"]["coverage"]["capabilities"]
                            if capability["state"] != "available"
                        ],
                        "dataset_gaps": [
                            {
                                "dataset_id": state["dataset_id"],
                                "label": state["label"],
                                "state": state["state"],
                                "reason": state["reason"],
                            }
                            for state in module["evidence"]["dataset_states"]
                            if state["state"] not in {"current", "unavailable"}
                        ],
                    }
                    for module in modules
                    if module["module_id"] in blocked
                ],
            }
            with self._session() as repos, repos.transaction():
                repos.macro.upsert_judgment_status(
                    session_date=session_date,
                    judgment_cutoff_ms=cutoff_ms,
                    state="blocked",
                    reason_code="critical_evidence_blocked",
                    details=blocker_details,
                    attempted_at_ms=now,
                )
            return {
                "status": "blocked",
                "session_date": str(session_date),
                "blocked_modules": blocked,
            }
        for module in modules:
            module["status"]["judgment"] = {"state": "current", "cutoff_ms": cutoff_ms}

        latest_fact_at_ms = max((int(module["latest_fact_at_ms"]) for module in modules), default=0)
        pack_payload = {
            "schema_version": _PACK_SCHEMA,
            "session_date": str(session_date),
            "judgment_cutoff_ms": cutoff_ms,
            "latest_fact_at_ms": latest_fact_at_ms,
            "compiler_version": _COMPILER_VERSION,
            "modules": modules,
            "calculation_registry": [
                {
                    "feature_id": spec.feature_id,
                    "module_id": spec.module_id,
                    "input_dataset_ids": list(spec.input_dataset_ids),
                    "formula_version": spec.formula_version,
                    "unit": spec.unit,
                    "windows": list(spec.windows),
                    "minimum_observations": spec.minimum_observations,
                    "gap_policy": spec.gap_policy,
                    "freshness_seconds": spec.freshness_seconds,
                    "baseline": spec.baseline,
                    "output_schema": spec.output_schema,
                }
                for spec in CALCULATION_REGISTRY.values()
            ],
            "changes_since_judgment": [
                {
                    "module_id": module["module_id"],
                    "changes": module["summary"]["top_changes"][:3],
                }
                for module in modules
            ],
        }
        pack_hash = _payload_hash(pack_payload)
        pack_id = "mep_" + pack_hash.removeprefix("sha256:")[:32]
        judgment = compile_daily_judgment(pack_payload)
        judgment_hash = _payload_hash(judgment)
        memo = render_daily_memo(judgment)
        with self._session() as repos, repos.transaction():
            pack_written = repos.macro.insert_evidence_pack(
                evidence_pack_id=pack_id,
                session_date=session_date,
                judgment_cutoff_ms=cutoff_ms,
                latest_fact_at_ms=latest_fact_at_ms,
                schema_version=_PACK_SCHEMA,
                compiler_version=_COMPILER_VERSION,
                payload=pack_payload,
                payload_hash=pack_hash,
                created_at_ms=now,
            )
            judgment_written = repos.macro.insert_daily_judgment(
                session_date=session_date,
                evidence_pack_id=pack_id,
                judgment_cutoff_ms=cutoff_ms,
                latest_fact_at_ms=latest_fact_at_ms,
                judgment=judgment,
                memo_text=memo,
                schema_version=_JUDGMENT_SCHEMA,
                compiler_version=_COMPILER_VERSION,
                payload_hash=judgment_hash,
                published_at_ms=now,
            )
            repos.macro.upsert_judgment_status(
                session_date=session_date,
                judgment_cutoff_ms=cutoff_ms,
                state="current",
                reason_code="judgment_published",
                details={"evidence_pack_id": pack_id},
                attempted_at_ms=now,
            )
        return {
            "status": "published" if judgment_written else "exists",
            "session_date": str(session_date),
            "evidence_pack_id": pack_id,
            "pack_rows_written": pack_written,
            "judgment_rows_written": judgment_written,
        }

    def _compile_modules(self, *, cutoff_ms: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        all_specs = tuple(DATASET_REGISTRY.values())
        series_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "series")
        market_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "market_observation")
        position_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "market_position")
        settlement_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "market_settlement")
        release_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "release")
        document_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "document")
        with self._session() as repos, repos.transaction():
            series_rows = repos.macro.series_history(
                dataset_ids=series_ids,
                limit_per_dataset=10_000,
                received_before_ms=cutoff_ms,
            )
            market_rows = repos.macro_market.market_history(
                dataset_ids=market_ids,
                limit_per_dataset=5_000,
                received_before_ms=cutoff_ms,
            )
            position_rows = repos.macro_market.position_history(
                dataset_ids=position_ids,
                received_before_ms=cutoff_ms,
            )
            settlement_rows = repos.macro_market.settlement_history(
                dataset_ids=settlement_ids,
                received_before_ms=cutoff_ms,
            )
            release_rows = repos.macro.release_history(
                dataset_ids=release_ids,
                received_before_ms=cutoff_ms,
            )
            document_rows = repos.macro.document_history(
                dataset_ids=document_ids,
                received_before_ms=cutoff_ms,
            )
            role_rows = repos.macro.fed_official_role_history(received_before_ms=cutoff_ms)
            analysis_rows = repos.macro.document_analysis_history(received_before_ms=cutoff_ms)
            analysis_job_state = repos.macro.document_analysis_job_state(received_before_ms=cutoff_ms)
            target_states = repos.macro.target_states()
        features = calculate_features(series_rows)
        modules = [
            build_typed_module_payload(
                module_id=module_id,
                now_ms=cutoff_ms,
                series_rows=series_rows,
                market_rows=market_rows,
                position_rows=position_rows,
                settlement_rows=settlement_rows,
                release_rows=release_rows,
                document_rows=document_rows,
                target_states=target_states,
                role_rows=role_rows,
                analysis_rows=analysis_rows,
                analysis_job_state=analysis_job_state,
            )
            for module_id in MACRO_MODULE_IDS
        ]
        return modules, features

    def _session(self) -> Any:
        return self.db.worker_session(
            self.worker_name,
            statement_timeout_seconds=float(self.settings.statement_timeout_seconds),
        )


def compile_daily_judgment(evidence_pack: dict[str, Any]) -> dict[str, Any]:
    modules = {module["module_id"]: module for module in evidence_pack["modules"]}
    dimensions = {
        "growth": _growth_state(modules["economy_inflation"]),
        "inflation": _inflation_state(modules["economy_inflation"]),
        "policy": _policy_state(modules["rates_fed"]),
        "liquidity": _liquidity_state(modules["liquidity_funding"]),
        "credit": _credit_state(modules["credit"]),
        "volatility": _volatility_state(modules["volatility"]),
    }
    directions = _asset_directions(modules)
    dominant_pressures = [
        {
            "dimension": dimension,
            "state": payload["state"],
            "driver": payload["driver"],
        }
        for dimension, payload in dimensions.items()
        if payload["state"] not in {"stable", "neutral", "normal"}
    ][:3]
    top_changes = [
        {"module_id": module["module_id"], **change}
        for module in evidence_pack["modules"]
        for change in module["summary"]["top_changes"][:1]
    ]
    contradictions = [
        {"module_id": module["module_id"], "text": text}
        for module in evidence_pack["modules"]
        for text in module["contradictions"]
    ]
    gaps = [
        {
            "module_id": module["module_id"],
            "label": capability["label"],
            "reason": capability["reason"] or capability["state"],
            "capability_id": capability["capability_id"],
        }
        for module in evidence_pack["modules"]
        for capability in module["status"]["coverage"]["capabilities"]
        if capability["state"] != "available"
    ]
    gaps.extend(
        {
            "module_id": module["module_id"],
            "dataset_id": state["dataset_id"],
            "label": state["label"],
            "reason": state["reason"],
        }
        for module in evidence_pack["modules"]
        for state in module["evidence"]["dataset_states"]
        if state["state"] not in {"current", "unavailable"}
    )
    citations = [
        {
            "citation_id": f"cite_{index + 1}",
            "dataset_id": fact["dataset_id"],
            "fact_ref": fact["fact_ref"],
            "reference": fact["reference"],
            "source_url": fact["source_url"],
        }
        for index, fact in enumerate(
            fact
            for module in evidence_pack["modules"]
            for fact in module["evidence"]["latest_facts"]
            if fact["fact_ref"]
        )
    ]
    return {
        "schema_version": _JUDGMENT_SCHEMA,
        "session_date": evidence_pack["session_date"],
        "judgment_cutoff_ms": evidence_pack["judgment_cutoff_ms"],
        "latest_fact_at_ms": evidence_pack["latest_fact_at_ms"],
        "overall_state": _overall_state(dimensions),
        "dominant_pressures": dominant_pressures,
        "top_3_changes": top_changes[:3],
        "dimensions": dimensions,
        "module_judgments": [
            {
                "module_id": module["module_id"],
                "coverage_state": module["status"]["coverage"]["state"],
                "data_health_state": module["status"]["data_health"]["state"],
                "summary": module["summary"]["headline"],
                "driver": (module["summary"]["top_changes"] or [None])[0],
            }
            for module in evidence_pack["modules"]
        ],
        "asset_directions": directions,
        "contradictions": contradictions,
        "falsifiers": [
            {"module_id": module["module_id"], "items": module["falsifiers"]} for module in evidence_pack["modules"]
        ],
        "next_checkpoints": [
            {"module_id": module["module_id"], **checkpoint}
            for module in evidence_pack["modules"]
            for checkpoint in module["next_checkpoints"][:2]
        ],
        "gaps": gaps,
        "citations": citations,
    }


def render_daily_memo(judgment: dict[str, Any]) -> str:
    lines = [
        f"# 每日宏观判断｜{judgment['session_date']}",
        "",
        f"判断截点：{judgment['judgment_cutoff_ms']}；最新事实：{judgment['latest_fact_at_ms']}。",
        "",
        f"总体状态：{judgment['overall_state']}。",
        "",
        "## 六维状态",
        "",
    ]
    for name, payload in judgment["dimensions"].items():
        lines.append(f"- {name}: {payload['state']}｜{payload['driver']}")
    lines.extend(["", "## 固定资产方向", ""])
    for asset, payload in judgment["asset_directions"].items():
        lines.append(f"- {asset}: 1周 {payload['1w']}；1月 {payload['1m']}；置信度 {payload['confidence']}。")
    if judgment["gaps"]:
        lines.extend(["", "## 数据缺口", ""])
        lines.extend(f"- {gap['module_id']} / {gap['label']}: {gap['reason']}" for gap in judgment["gaps"])
    return "\n".join(lines)


def _growth_state(module: dict[str, Any]) -> dict[str, str]:
    payroll = _change_for(module, "fred.payems")
    unemployment = _change_for(module, "fred.unrate")
    if payroll is None and unemployment is None:
        return _state("stable", "就业与增长事实不足")
    if (payroll or 0) < 0 or (unemployment or 0) > 0.2:
        return _state("slowing", "就业增量走弱或失业率上升")
    if (payroll or 0) > 0 and (unemployment or 0) <= 0:
        return _state("accelerating", "就业总量增加且失业率未上升")
    return _state("stable", "增长证据未形成一致方向")


def _inflation_state(module: dict[str, Any]) -> dict[str, str]:
    cpi = _year_over_year_for(module["inflation"]["indicators"], "fred.cpiaucsl")
    pce = _year_over_year_for(module["inflation"]["indicators"], "fred.pcepilfe")
    if cpi is None and pce is None:
        return _state("sticky", "通胀同比特征不足")
    average = sum(value for value in (cpi, pce) if value is not None) / sum(value is not None for value in (cpi, pce))
    if average > 3.0:
        return _state("sticky", "主要通胀同比仍高于2%目标")
    if average < 1.5:
        return _state("disinflating", "主要通胀同比已显著回落")
    return _state("disinflating", "主要通胀同比接近但仍高于目标")


def _policy_state(module: dict[str, Any]) -> dict[str, str]:
    dgs2 = _change_for(module, "fred.dgs2")
    if dgs2 is None:
        return _state("transition", "短端利率变化不足")
    if dgs2 > 0.15:
        return _state("tightening", "2年期收益率短窗口上行")
    if dgs2 < -0.15:
        return _state("easing", "2年期收益率短窗口下行")
    return _state("neutral", "短端利率处于区间")


def _liquidity_state(module: dict[str, Any]) -> dict[str, str]:
    fed_assets = _change_for(module, "fred.walcl")
    reserves = _change_for(module, "fred.wrbwfrbl")
    if fed_assets is None and reserves is None:
        return _state("neutral", "央行资产与准备金变化不足")
    if (fed_assets or 0) > 0 and (reserves or 0) >= 0:
        return _state("expanding", "央行资产或准备金增加")
    if (fed_assets or 0) < 0 and (reserves or 0) < 0:
        return _state("draining", "央行资产与准备金共同下降")
    return _state("neutral", "流动性分项方向不一致")


def _credit_state(module: dict[str, Any]) -> dict[str, Any]:
    dimensions = list(module["cycle_dimensions"])
    stressed_states = {"stressed", "tightening", "expensive", "restrictive", "deteriorating"}
    easing_states = {"easing", "cheap", "improving"}
    pressured = [dimension for dimension in dimensions if dimension["state"] in stressed_states]
    easing = [dimension for dimension in dimensions if dimension["state"] in easing_states]
    conflicts = [conflict for dimension in dimensions for conflict in dimension["conflicts"]]
    if len(pressured) >= 2:
        state = "stressed"
        driver = "；".join(str(dimension["driver"]) for dimension in pressured[:2])
    elif pressured:
        state = "tightening"
        driver = str(pressured[0]["driver"])
    elif easing:
        state = "easing"
        driver = str(easing[0]["driver"])
    else:
        state = "neutral"
        driver = "五个信用维度未形成一致压力"
    return {
        "state": state,
        "driver": driver,
        "subdimensions": dimensions,
        "conflicts": conflicts,
    }


def _volatility_state(module: dict[str, Any]) -> dict[str, str]:
    vix = _latest_for(module, "fred.vixcls")
    if vix is None:
        return _state("normal", "VIX事实不足")
    if vix >= 30:
        return _state("stressed", "VIX高于30")
    if vix >= 22:
        return _state("elevated", "VIX高于22")
    if vix <= 13:
        return _state("complacent", "VIX低于13")
    return _state("normal", "VIX处于常态区间")


def _asset_directions(modules: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    cross_asset = modules["cross_asset"]
    rows = {
        item["dataset_id"]: item
        for item in (
            *cross_asset["assets"]["proxies"],
            *cross_asset["assets"]["benchmarks"],
        )
    }
    result = {}
    for asset, dataset_id in _FIXED_ASSETS.items():
        change = rows.get(dataset_id)
        spec = DATASET_REGISTRY[dataset_id]
        if change is None:
            one_week = one_month = "no_call"
            conflicts = ["缺少可用价格历史"]
        else:
            one_week = _direction(change.get("change_1w_pct", change.get("change_1w")))
            one_month = _direction(change.get("change_1m_pct", change.get("change_1m")))
            conflicts = [] if one_week == one_month else ["1周与1月方向不一致"]
        result[asset] = {
            "1w": one_week,
            "1m": one_month,
            "drivers": [change["label"]] if change else [],
            "conflicts": conflicts,
            "invalidation": "对应窗口价格方向反转",
            "confidence": "low" if spec.trust_tier == "untrusted_proxy" else "medium",
            "dataset_id": dataset_id,
        }
    return result


def _overall_state(dimensions: dict[str, dict[str, Any]]) -> str:
    stressed = [
        name
        for name, payload in dimensions.items()
        if payload["state"] in {"contraction", "stressed", "tightening", "elevated"}
    ]
    if len(stressed) >= 3:
        return "宏观压力共振"
    if stressed:
        return "分项压力、尚未共振"
    return "宏观状态中性"


def _state(state: str, driver: str) -> dict[str, str]:
    return {"state": state, "driver": driver}


def _change_for(module: dict[str, Any], dataset_id: str) -> float | None:
    for change in module["summary"]["top_changes"]:
        if change["dataset_id"] == dataset_id and change["change_1w"] is not None:
            return float(change["change_1w"])
    return None


def _latest_for(module: dict[str, Any], dataset_id: str) -> float | None:
    for fact in module["evidence"]["latest_facts"]:
        if fact["dataset_id"] == dataset_id and fact["value"] is not None:
            return float(fact["value"])
    return None


def _year_over_year_for(indicators: list[dict[str, Any]], dataset_id: str) -> float | None:
    indicator = next((item for item in indicators if item["dataset_id"] == dataset_id), None)
    if indicator is None:
        return None
    history = indicator["history"]
    if len(history) < 13:
        return None
    latest = float(history[-1]["value"])
    prior = float(history[-13]["value"])
    return (latest / prior - 1) * 100 if prior else None


def _direction(value: float | None) -> str:
    if value is None:
        return "no_call"
    if abs(value) < 1e-9:
        return "range"
    return "up" if value > 0 else "down"


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1_000)


def resolve_judgment_session(*, now_ms: int) -> date:
    """Return the trading session whose 08:50 judgment should be visible now."""

    instant = datetime.fromtimestamp(int(now_ms) / 1_000, tz=_NEW_YORK)
    candidate = instant.date()
    if is_us_market_session(candidate) and instant.timetz().replace(tzinfo=None) >= _JUDGMENT_TIME:
        return candidate
    candidate -= timedelta(days=1)
    while not is_us_market_session(candidate):
        candidate -= timedelta(days=1)
    return candidate


__all__ = [
    "MacroJudgmentService",
    "compile_daily_judgment",
    "render_daily_memo",
    "resolve_judgment_session",
]
