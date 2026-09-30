"""Compact, frozen model input built only from one Case's point-in-time LIVE facts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from .forecast import LegProbabilities
from .paper import LegGeometry

VIEW_VERSION = "case_view_v2"
_HEX64 = re.compile(r"\b[0-9a-fA-F]{64}\b")
_MILLISECONDS = re.compile(r"\b1[0-9]{12}\b")
_FEATURE_EXCLUDE = frozenset({"profile_version", "source_kind", "source_venue", "data_status"})
_NEWS_LABELS = ("mode", "phase", "polarity", "content_kind", "change_kind")


@dataclass(frozen=True, slots=True)
class BaseRates:
    side: Literal["long", "short"]
    matured_legs: int
    probabilities: LegProbabilities | None


@dataclass(frozen=True, slots=True)
class CaseView:
    case_id: str
    asset_id: str
    trigger_kind: Literal["oi", "catalyst"]
    decided_at_ms: int
    model_input: dict[str, Any]
    features: dict[str, str | None]
    geometry: LegGeometry
    half_spread_bps: Decimal
    base_rates: tuple[BaseRates, BaseRates]
    version: str = VIEW_VERSION
    evidence_refs: dict[str, str] | None = None
    paper_contract: dict[str, str] | None = None
    intake_context: dict[str, Any] | None = None

    def prompt_json(self) -> str:
        """Expose semantic asset and relative ages; durable identities stay in PG."""
        return json.dumps(self.model_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _clean(value: object, *, limit: int) -> str:
    return _MILLISECONDS.sub("[clock]", _HEX64.sub("[digest]", str(value)))[:limit]


def _rates(value: BaseRates) -> dict[str, object]:
    probabilities = value.probabilities
    return {
        "side": value.side,
        "n": value.matured_legs,
        "p": None
        if probabilities is None
        else {"tp": str(probabilities.p_tp), "sl": str(probabilities.p_sl), "timeout": str(probabilities.p_timeout)},
    }


def _typed_claims(payload: dict[str, Any]) -> list[dict[str, Any]]:
    changes = {
        item.get("current_ref"): item.get("kind") for item in payload.get("changes", ()) if isinstance(item, dict)
    }
    result = []
    for claim in payload.get("claims", ())[:12]:
        if not isinstance(claim, dict):
            continue
        fields = claim.get("fields") or {}
        if not isinstance(fields, dict):
            continue
        item: dict[str, Any] = {
            key: _clean(fields[key], limit=160)
            for key in (
                *_NEWS_LABELS,
                "subject",
                "action",
                "object",
                "speaker",
                "effective_at",
                "occurred_at",
                "statistical_period",
            )
            if fields.get(key) is not None
        }
        item["statement"] = _clean(claim.get("statement", ""), limit=240)
        item["conditions"] = [_clean(value, limit=120) for value in fields.get("conditions", ())[:4]]
        item["quantities"] = fields.get("quantities", ())[:4]
        item["projection_coverage"] = {
            "statement_truncated": len(_clean(claim.get("statement", ""), limit=1_000_000)) > 240,
            "fields_truncated": sum(
                len(_clean(fields[key], limit=1_000_000)) > 160
                for key in item
                if key in fields and key not in ("conditions", "quantities", "statement")
            ),
            "conditions_truncated": sum(
                len(_clean(value, limit=1_000_000)) > 120 for value in fields.get("conditions", ())[:4]
            ),
            "conditions_omitted": max(0, len(fields.get("conditions", ())) - 4),
            "quantities_omitted": max(0, len(fields.get("quantities", ())) - 4),
        }
        if changes.get(claim.get("ref")) is not None:
            item["change_kind"] = _clean(changes[claim.get("ref")], limit=80)
        result.append(item)
    return result


def build_case_view(
    *,
    case_id: str,
    asset_id: str,
    trigger_kind: Literal["oi", "catalyst"],
    decided_at_ms: int,
    source_fact: dict[str, Any],
    features: dict[str, Any],
    geometry: LegGeometry,
    half_spread_bps: Decimal,
    base_rates: tuple[BaseRates, BaseRates],
    recent_context: tuple[dict[str, Any], ...] = (),
    native_symbol: str | None = None,
    units_per_contract: Decimal | None = None,
    observations: dict[str, Any] | None = None,
    episode: dict[str, Any] | None = None,
) -> CaseView:
    if not case_id or not asset_id or decided_at_ms <= 0 or half_spread_bps < 0:
        raise ValueError("case_view_identity_invalid")
    if tuple(item.side for item in base_rates) != ("long", "short"):
        raise ValueError("case_view_baseline_sides_invalid")
    compact_features: dict[str, str | None] = {}
    evidence: list[dict[str, str]] = []
    for key in sorted(features):
        if key in _FEATURE_EXCLUDE:
            continue
        raw = features[key]
        value = None if raw is None else _clean(raw, limit=80)
        compact_features[key] = value
        if value is not None:
            evidence.append(
                {
                    "ref": f"e{len(evidence) + 1}",
                    "field": key,
                    "value": value,
                    "unit": "bps" if key.endswith("_bps") else "USD" if key.endswith("_usd") else "value",
                }
            )
    evidence_refs: dict[str, str] = {}
    source: dict[str, Any] = {
        key: _clean(source_fact[key], limit=80) for key in _NEWS_LABELS if source_fact.get(key) is not None
    }
    if trigger_kind == "catalyst":
        source["claims"] = _typed_claims(source_fact)
        source["claims_omitted"] = max(0, len(source_fact.get("claims", ())) - len(source["claims"]))
        raw_claims = [
            claim
            for claim in source_fact.get("claims", ())[:12]
            if isinstance(claim, dict) and isinstance(claim.get("fields") or {}, dict)
        ]
        source["projection_incomplete"] = any(any(claim["projection_coverage"].values()) for claim in source["claims"])
        for claim, raw in zip(source["claims"], raw_claims, strict=True):
            ref = f"e{len(evidence) + 1}"
            claim["ref"] = ref
            evidence.append({"ref": ref, "field": "catalyst_claim", "value": claim["statement"], "unit": "statement"})
            evidence_refs[ref] = str(raw.get("ref", ""))
        if source_fact.get("text"):
            source["text"] = _clean(source_fact["text"], limit=1_200)
    elif trigger_kind == "oi":
        for key in ("oi_change_bps", "measurement_definition", "measurement_window_ms", "direction", "source_venue"):
            if source_fact.get(key) is not None:
                source[key] = _clean(source_fact[key], limit=120)
    if trigger_kind == "oi" and source_fact.get("oi_change_bps") is not None:
        ref = f"e{len(evidence) + 1}"
        source["ref"] = ref
        evidence.append(
            {"ref": ref, "field": "source_oi_change_bps", "value": str(source_fact["oi_change_bps"]), "unit": "bps"}
        )
        evidence_refs[ref] = str(source_fact.get("evidence_ref", ""))
    source["ages_s"] = {
        name: None
        if source_fact.get(name) is None or int(source_fact[name]) > decided_at_ms
        else (decided_at_ms - int(source_fact[name])) // 1000
        for name in ("provider_event_at_ms", "source_received_at_ms", "source_recorded_at_ms", "first_available_at_ms")
    }
    source["ingest_mode"] = source_fact.get("ingest_mode", "unknown")
    context: list[dict[str, object]] = []
    for fact in recent_context[:5]:
        payload = fact["payload"]
        item: dict[str, object] = {
            "kind": fact["kind"],
            "claims": _typed_claims(payload),
            "age_s": None
            if fact.get("first_visible_at_ms") is None
            else max(0, (decided_at_ms - int(fact["first_visible_at_ms"])) // 1000),
            "oi_change_bps": payload.get("oi_change_bps"),
            "measurement_definition": payload.get("measurement_definition"),
            "direction": payload.get("direction"),
        }
        if payload.get("text"):
            item["text"] = _clean(payload["text"], limit=360)
        item["amendments"] = [
            {
                "claims": _typed_claims(amendment["payload"]),
                "text": _clean(amendment["payload"]["text"], limit=360) if amendment["payload"].get("text") else None,
                "retired_claims": len(amendment["payload"].get("retired_claim_refs") or ()),
            }
            for amendment in fact.get("amendments", ())[:3]
        ]
        ref = f"e{len(evidence) + 1}"
        item["ref"] = ref
        evidence.append({"ref": ref, "field": "recent_source", "value": _clean(item, limit=360), "unit": "observation"})
        evidence_refs[ref] = str(fact.get("trigger_id", ""))
        context.append(item)
    model_input = {
        "view": VIEW_VERSION,
        "feature_contract": features.get("profile_version"),
        "trigger": trigger_kind,
        "asset": {
            "asset_id": asset_id,
            "native_symbol": native_symbol,
            "units_per_contract": None if units_per_contract is None else str(units_per_contract),
            "price_unit": "native_quote",
            "quantity_unit": "native_base",
        },
        "observations": observations or {"data_status": features.get("data_status", {})},
        "episode": episode or {"role": "unknown"},
        "source": source,
        "recent_context": context,
        "facts": evidence,
        "legs": {
            "long": {
                "stop_bps": geometry.stop_bps,
                "tp_bps": geometry.tp_bps,
                "max_hold_s": geometry.max_hold_ms // 1000,
            },
            "short": {
                "stop_bps": geometry.stop_bps,
                "tp_bps": geometry.tp_bps,
                "max_hold_s": geometry.max_hold_ms // 1000,
            },
            "round_trip_cost_bps": str(Decimal(10) + half_spread_bps),
        },
        "base_rates": [_rates(item) for item in base_rates],
    }
    payload = json.dumps(model_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(payload.encode()) > 16_384:
        raise ValueError("case_view_prompt_oversized")
    return CaseView(
        case_id,
        asset_id,
        trigger_kind,
        decided_at_ms,
        model_input,
        compact_features,
        geometry,
        half_spread_bps,
        base_rates,
        VIEW_VERSION,
        evidence_refs,
        {
            "version": "paper_cost_v1",
            "taker_fee_bps": "5",
            "spread": "entry_half_spread_once",
            "anchor": "first_later_closed_live_1m_bar",
            "barrier": "sl_wins_same_bar",
        },
    )


def case_view_from_record(record: dict[str, Any]) -> CaseView:
    """Restore the exact persisted input for crash recovery and offline replay."""
    geometry_record = record["geometry"]
    baselines = []
    for item in record["base_rates"]:
        probabilities = item["probabilities"]
        baselines.append(
            BaseRates(
                item["side"],
                int(item["matured_legs"]),
                None
                if probabilities is None
                else LegProbabilities(
                    Decimal(str(probabilities["p_tp"])),
                    Decimal(str(probabilities["p_sl"])),
                    Decimal(str(probabilities["p_timeout"])),
                ),
            )
        )
    if len(baselines) != 2 or tuple(item.side for item in baselines) != ("long", "short"):
        raise ValueError("case_view_baseline_sides_invalid")
    return CaseView(
        case_id=str(record["case_id"]),
        asset_id=str(record["asset_id"]),
        trigger_kind=record["trigger_kind"],
        decided_at_ms=int(record["decided_at_ms"]),
        model_input=dict(record["model_input"]),
        features=dict(record["features"]),
        geometry=LegGeometry(
            int(geometry_record["stop_bps"]),
            int(geometry_record["tp_bps"]),
            int(geometry_record["max_hold_ms"]),
            str(geometry_record["version"]),
        ),
        half_spread_bps=Decimal(str(record["half_spread_bps"])),
        base_rates=(baselines[0], baselines[1]),
        version=str(record["version"]),
        evidence_refs=dict(record["evidence_refs"]) if record.get("evidence_refs") is not None else None,
        paper_contract=dict(record["paper_contract"]) if record.get("paper_contract") is not None else None,
        intake_context=dict(record["intake_context"]) if record.get("intake_context") is not None else None,
    )
