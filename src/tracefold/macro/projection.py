from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from tracefold.macro.calculations import calculate_features
from tracefold.macro.domain import MACRO_MODULE_IDS, MACRO_MODULE_LABELS, MacroModuleId
from tracefold.macro.registry import datasets_for_module

MODULE_CHARTS: dict[MacroModuleId, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "rates_fed": (
        ("美国国债收益率曲线", ("fred.dgs2", "fred.dgs10", "fred.dgs30")),
        ("实际利率与通胀补偿", ("fred.dfii10", "fred.t10yie")),
        ("政策利率走廊", ("fred.effr", "fred.dfedtaru", "fred.dfedtarl")),
        ("利率期货杠杆资金净仓位（占OI）", ("cftc.tff.rates_positions",)),
    ),
    "economy_inflation": (
        ("通胀水平", ("fred.cpiaucsl", "fred.cpilfesl", "fred.pcepi", "fred.pcepilfe")),
        ("就业总量", ("fred.payems", "fred.unrate")),
        ("增长与需求", ("fred.gdpc1", "fred.rsafs", "fred.indpro")),
        ("就业领先", ("fred.icsa",)),
    ),
    "liquidity_funding": (
        ("央行与财政流动性", ("fred.walcl", "fred.wtregen", "fred.rrpontsyd")),
        ("银行准备金", ("fred.wrbwfrbl",)),
        ("资金利率走廊", ("fred.sofr", "fred.iorb")),
    ),
    "credit": (
        ("投资级与高收益利差", ("fred.bamlc0a0cm", "fred.bamlh0a0hym2")),
        ("信用尾部", ("fred.bamlc0a4cbbb", "fred.bamlh0a3hyc")),
        ("金融与信贷条件", ("fred.nfci", "fred.drtscilm")),
        ("信用期货杠杆资金净仓位（占OI）", ("cftc.tff.credit_positions",)),
    ),
    "volatility": (
        ("VIX期限结构", ("fred.vixcls", "fred.vxvcls")),
        ("跨资产隐含波动率", ("fred.vxncls", "fred.gvzcls", "fred.ovxcls")),
    ),
    "cross_asset": (
        (
            "固定决策资产（前收盘）",
            (
                "nasdaq.spy.history",
                "nasdaq.tlt.history",
                "nasdaq.hyg.history",
                "nasdaq.dxy.history",
            ),
        ),
        (
            "商品与加密资产",
            ("nasdaq.gld.history", "nasdaq.uso.history", "binance.btcusdt.spot"),
        ),
        ("VIX期货官方结算", ("cboe.cfe.vx.settlement",)),
        ("跨资产期货杠杆资金净仓位（占OI）", ("cftc.tff.cross_asset_positions",)),
    ),
}


class MacroProjectionService:
    def __init__(
        self,
        *,
        db: Any,
        settings: Any,
        worker_name: str = "macro_projection",
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.worker_name = worker_name
        self.clock_ms = clock_ms or _now_ms

    def rebuild(self, *, now_ms: int | None = None) -> dict[str, Any]:
        now = int(now_ms if now_ms is not None else self.clock_ms())
        all_specs = tuple(spec for module in MACRO_MODULE_IDS for spec in datasets_for_module(module))
        series_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "series")
        market_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "market_observation")
        position_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "market_position")
        settlement_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "market_settlement")
        release_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "release")
        document_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "document")
        with self._session() as repos, repos.transaction():
            series_rows = repos.macro.series_history(dataset_ids=series_ids)
            market_rows = repos.macro_market.market_history(dataset_ids=market_ids)
            position_rows = repos.macro_market.position_history(dataset_ids=position_ids)
            settlement_rows = repos.macro_market.settlement_history(dataset_ids=settlement_ids)
            release_rows = repos.macro.release_history(dataset_ids=release_ids)
            document_rows = repos.macro.document_history(dataset_ids=document_ids)
            target_states = repos.macro.target_states()
            features = calculate_features(series_rows)
            feature_writes = 0
            for feature in features:
                feature_writes += repos.macro.upsert_feature(
                    feature_id=feature["feature_id"],
                    as_of_date=feature["as_of_date"],
                    formula_version=feature["formula_version"],
                    value_numeric=feature["value_numeric"],
                    unit=feature["unit"],
                    inputs=feature["inputs"],
                    payload_hash=feature["payload_hash"],
                    computed_at_ms=now,
                )
            module_writes = 0
            for module_id in MACRO_MODULE_IDS:
                payload = build_module_payload(
                    module_id=module_id,
                    now_ms=now,
                    series_rows=series_rows,
                    market_rows=market_rows,
                    position_rows=position_rows,
                    settlement_rows=settlement_rows,
                    release_rows=release_rows,
                    document_rows=document_rows,
                    target_states=target_states,
                    features=features,
                )
                payload_hash = _payload_hash(payload)
                module_writes += repos.macro.upsert_module_current(
                    module_id=module_id,
                    readiness=payload["readiness"],
                    fact_cutoff_ms=payload["latest_fact_at_ms"],
                    payload=payload,
                    payload_hash=payload_hash,
                    updated_at_ms=now,
                )
        return {
            "features_computed": len(features),
            "feature_rows_written": feature_writes,
            "modules_computed": len(MACRO_MODULE_IDS),
            "module_rows_written": module_writes,
        }

    def _session(self) -> Any:
        return self.db.worker_session(
            self.worker_name,
            statement_timeout_seconds=float(self.settings.statement_timeout_seconds),
        )


def build_module_payload(
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
    features: list[dict[str, Any]],
) -> dict[str, Any]:
    specs = datasets_for_module(module_id)
    dataset_ids = {spec.dataset_id for spec in specs}
    module_series = [row for row in series_rows if row["dataset_id"] in dataset_ids]
    module_market = [row for row in market_rows if row["dataset_id"] in dataset_ids]
    module_positions = [row for row in position_rows if row["dataset_id"] in dataset_ids]
    module_settlements = [row for row in settlement_rows if row["dataset_id"] in dataset_ids]
    module_releases = [row for row in release_rows if row["dataset_id"] in dataset_ids]
    module_documents = [row for row in document_rows if row["dataset_id"] in dataset_ids]
    states_by_dataset = {
        str(row["dataset_id"]): row for row in target_states if row["dataset_id"] in dataset_ids
    }
    latest_by_dataset = _latest_by_dataset(
        module_series,
        module_market,
        module_positions,
        module_settlements,
        module_releases,
        module_documents,
    )
    dataset_states = [
        _dataset_state(spec, states_by_dataset.get(spec.dataset_id), latest_by_dataset.get(spec.dataset_id), now_ms)
        for spec in specs
    ]
    readiness = _module_readiness(dataset_states)
    latest_fact_at_ms = max(
        (int(fact["received_at_ms"]) for fact in latest_by_dataset.values()),
        default=0,
    )
    changes = _top_changes(specs, module_series, module_market, module_settlements)
    charts = [
        _chart_payload(
            title,
            ids,
            module_series,
            module_market,
            module_positions,
            module_settlements,
        )
        for title, ids in MODULE_CHARTS[module_id]
    ]
    raw_latest = list(latest_by_dataset.values())
    seen_fact_refs = {_fact_identity(row) for row in raw_latest}
    raw_latest.extend(
        row
        for row in _latest_positions_by_contract(module_positions)
        if _fact_identity(row) not in seen_fact_refs
    )
    module_features = [
        {
            key: (str(value) if isinstance(value, date) else value)
            for key, value in feature.items()
        }
        for feature in features
        if feature["module_id"] == module_id
    ]
    gaps = [
        {
            "dataset_id": state["dataset_id"],
            "label": state["label"],
            "state": state["state"],
            "reason": state["reason"],
            "decision_impact": "blocks_new_judgment" if state["critical"] else "reduces_confidence",
        }
        for state in dataset_states
        if state["state"] not in {"current", "not_due"}
    ]
    return {
        "schema_version": "macro_module_v1",
        "module_id": module_id,
        "label": MACRO_MODULE_LABELS[module_id],
        "readiness": readiness,
        "judgment_cutoff_ms": None,
        "latest_fact_at_ms": latest_fact_at_ms,
        "current_state": _current_state(module_id, module_features, changes),
        "top_changes": changes[:5],
        "features": module_features,
        "charts": charts[:4],
        "contradictions": _contradictions(module_id, module_features, changes),
        "falsifiers": _falsifiers(module_id),
        "next_checkpoints": _next_checkpoints(dataset_states),
        "gaps": gaps,
        "dataset_states": dataset_states,
        "raw_evidence": [
            _fact_summary(fact)
            for fact in sorted(raw_latest, key=lambda row: (str(row["dataset_id"]), _fact_reference(row) or ""))
        ],
    }


def _dataset_state(
    spec: Any,
    target: dict[str, Any] | None,
    latest: dict[str, Any] | None,
    now_ms: int,
) -> dict[str, Any]:
    if spec.adapter_id == "unavailable":
        state = "unavailable"
        reason = str(spec.metadata.get("unavailable_reason") or "not_configured")
    elif target is None:
        state = "invalid"
        reason = "acquisition_target_missing"
    elif latest is None:
        state = "backfilling" if target["status"] == "backfilling" else str(target["status"])
        reason = "no_valid_fact"
    else:
        fact_clock = _fact_clock_ms(latest)
        age_ms = max(0, now_ms - fact_clock)
        if age_ms > spec.freshness_seconds * 2_000:
            state = "stale"
            reason = "fact_past_freshness_budget"
        elif age_ms > spec.freshness_seconds * 1_000:
            state = "delayed"
            reason = "fact_delayed"
        else:
            state = "current"
            reason = "within_freshness_budget"
    return {
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


def _module_readiness(states: list[dict[str, Any]]) -> str:
    if any(
        state["critical"] and state["state"] in {"stale", "invalid", "unavailable", "backfilling", "pending"}
        for state in states
    ):
        return "blocked"
    if any(state["state"] != "current" for state in states):
        return "degraded"
    return "ready"


def _latest_by_dataset(*groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in (item for group in groups for item in group):
        dataset_id = str(row["dataset_id"])
        if dataset_id not in latest or _fact_order(row) > _fact_order(latest[dataset_id]):
            latest[dataset_id] = row
    return latest


def _top_changes(
    specs: tuple[Any, ...],
    series_rows: list[dict[str, Any]],
    market_rows: list[dict[str, Any]],
    settlement_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows_by_dataset: dict[str, list[dict[str, Any]]] = {}
    for row in (*series_rows, *market_rows, *settlement_rows):
        rows_by_dataset.setdefault(str(row["dataset_id"]), []).append(row)
    changes = []
    for spec in specs:
        rows = sorted(rows_by_dataset.get(spec.dataset_id, []), key=_fact_order)
        latest_value = _fact_value(rows[-1]) if rows else None
        if latest_value is None:
            continue
        one_back = _semantic_back(spec.frequency, "short")
        medium_back = _semantic_back(spec.frequency, "medium")
        short_change = _change(latest_value, rows, one_back)
        medium_change = _change(latest_value, rows, medium_back)
        changes.append(
            {
                "dataset_id": spec.dataset_id,
                "label": spec.label,
                "as_of": _fact_reference(rows[-1]),
                "value": latest_value,
                "unit": spec.unit,
                "short_window": _semantic_label(spec.frequency, "short"),
                "short_change": short_change,
                "medium_window": _semantic_label(spec.frequency, "medium"),
                "medium_change": medium_change,
                "magnitude": max(abs(short_change or 0.0), abs(medium_change or 0.0)),
                "source_url": rows[-1]["source_url"],
            }
        )
    return sorted(changes, key=lambda item: item["magnitude"], reverse=True)


def _chart_payload(
    title: str,
    dataset_ids: tuple[str, ...],
    *groups: list[dict[str, Any]],
) -> dict[str, Any]:
    points = []
    for row in (item for group in groups for item in group):
        if row["dataset_id"] not in dataset_ids:
            continue
        value = _fact_value(row)
        if value is None:
            continue
        series_id = (
            f"{row['dataset_id']}:{row['contract_code']}"
            if row.get("position_fact_id") is not None
            else str(row["dataset_id"])
        )
        points.append(
            {
                "dataset_id": series_id,
                "label": row.get("contract_name") or series_id,
                "x": _fact_reference(row),
                "y": value,
                "unit": row["unit"],
            }
        )
    available_series = {str(point["dataset_id"]) for point in points}
    series = [
        series_id
        for dataset_id in dataset_ids
        for series_id in sorted(
            candidate
            for candidate in available_series
            if candidate == dataset_id or candidate.startswith(f"{dataset_id}:")
        )
    ]
    return {"chart_id": _slug(title), "title": title, "series": series, "points": points}


def _current_state(
    module_id: MacroModuleId,
    features: list[dict[str, Any]],
    changes: list[dict[str, Any]],
) -> dict[str, Any]:
    dominant = max(changes, key=lambda item: item["magnitude"], default=None)
    return {
        "headline": f"{MACRO_MODULE_LABELS[module_id]}当前证据",
        "dominant_change": dominant,
        "feature_count": len(features),
        "interpretation": "仅陈述可复算事实；宏观判断见每日判断。",
    }


def _contradictions(
    module_id: MacroModuleId,
    features: list[dict[str, Any]],
    changes: list[dict[str, Any]],
) -> list[str]:
    if module_id == "rates_fed" and any(
        feature["feature_id"] == "rates.curve_10y2y" and feature["value_numeric"] < 0
        for feature in features
    ):
        return ["收益率曲线仍倒挂；与软着陆定价之间存在张力。"]
    if module_id == "cross_asset" and len(changes) >= 2:
        signs = {0 if item["short_change"] is None else item["short_change"] > 0 for item in changes[:5]}
        if len(signs) > 1:
            return ["固定观察资产在短窗口内方向不一致，不能用单一风险偏好叙事概括。"]
    return []


def _falsifiers(module_id: MacroModuleId) -> list[str]:
    return {
        "rates_fed": ["2年期收益率与政策预期反向突破最近一个月区间。"],
        "economy_inflation": ["连续两次核心通胀或就业发布改变当前方向。"],
        "liquidity_funding": ["SOFR越过政策走廊或准备金连续显著下降。"],
        "credit": ["高收益利差突破近一年压力分位且贷款标准继续收紧。"],
        "volatility": ["VIX期限结构从升水切换为持续倒挂。"],
        "cross_asset": ["固定观察资产的1周与1月方向同时反转。"],
    }[module_id]


def _next_checkpoints(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "dataset_id": state["dataset_id"],
            "label": state["label"],
            "next_check": "按 Dataset Registry 数据时钟自动检查",
        }
        for state in states
        if state["critical"] or state["state"] != "current"
    ][:6]


def _fact_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "dataset_id": row["dataset_id"],
        "label": row.get("contract_name") or row["dataset_id"],
        "fact_ref": (
            row.get("fact_id")
            or row.get("observation_id")
            or row.get("position_fact_id")
            or row.get("settlement_id")
            or row.get("release_fact_id")
            or row.get("document_id")
        ),
        "reference": _fact_reference(row),
        "value": _fact_value(row) if row.get("document_id") is None else row.get("title"),
        "unit": row.get("unit") or ("document" if row.get("document_id") else "unknown"),
        "published_at_ms": row.get("published_at_ms"),
        "received_at_ms": row["received_at_ms"],
        "source_url": row["source_url"],
    }


def _fact_value(row: dict[str, Any]) -> float | None:
    for key in (
        "value_numeric",
        "leveraged_net_pct_oi",
        "settlement_price",
        "actual_value",
    ):
        if row.get(key) is not None:
            return float(row[key])
    return None


def _fact_reference(row: dict[str, Any] | None) -> str | None:
    if row is None:
        return None
    if row.get("reference_date") is not None:
        return str(row["reference_date"])
    if row.get("trade_date") is not None:
        return str(row["trade_date"])
    if row.get("report_date") is not None:
        return str(row["report_date"])
    if row.get("reference_period") is not None:
        return str(row["reference_period"])
    if row.get("effective_date") is not None:
        return str(row["effective_date"])
    if row.get("observed_at_ms") is not None:
        return datetime.fromtimestamp(int(row["observed_at_ms"]) / 1_000, tz=UTC).date().isoformat()
    return None


def _fact_clock_ms(row: dict[str, Any]) -> int:
    if row.get("observed_at_ms") is not None:
        return int(row["observed_at_ms"])
    if row.get("published_at_ms") is not None:
        return int(row["published_at_ms"])
    reference = row.get("reference_date") or row.get("trade_date") or row.get("report_date")
    if isinstance(reference, date):
        return int(datetime(reference.year, reference.month, reference.day, tzinfo=UTC).timestamp() * 1_000)
    return int(row["received_at_ms"])


def _fact_order(row: dict[str, Any]) -> tuple[str, int]:
    return (_fact_reference(row) or "", int(row["received_at_ms"]))


def _latest_positions_by_contract(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["dataset_id"]), str(row["contract_code"]))
        if key not in latest or _fact_order(row) > _fact_order(latest[key]):
            latest[key] = row
    return list(latest.values())


def _fact_identity(row: dict[str, Any]) -> str:
    return str(
        row.get("fact_id")
        or row.get("observation_id")
        or row.get("position_fact_id")
        or row.get("settlement_id")
        or row.get("release_fact_id")
        or row.get("document_id")
        or ""
    )


def _semantic_back(frequency: str, window: str) -> int:
    if frequency in {"daily", "intraday"}:
        return 5 if window == "short" else 21
    if frequency == "weekly":
        return 1 if window == "short" else 4
    if frequency == "monthly":
        return 1 if window == "short" else 3
    return 1 if window == "short" else 4


def _semantic_label(frequency: str, window: str) -> str:
    count = _semantic_back(frequency, window)
    labels = {"daily": "交易日", "intraday": "交易日", "weekly": "周", "monthly": "月", "quarterly": "季"}
    return f"{count}{labels.get(frequency, '期')}"


def _change(latest: float, rows: list[dict[str, Any]], back: int) -> float | None:
    if len(rows) <= back:
        return None
    prior = _fact_value(rows[-back - 1])
    return round(latest - prior, 6) if prior is not None else None


def _slug(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = ["MacroProjectionService", "build_module_payload"]
