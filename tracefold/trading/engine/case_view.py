"""Compact, frozen model input built only from one Case's point-in-time LIVE facts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from .forecast import LegProbabilities
from .paper import LegGeometry

VIEW_VERSION = "case_view_v1"
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

    def prompt_json(self) -> str:
        """Case identity and clocks stay in PG; the assessor sees only compact aliases."""
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


def _typed_claims(payload: dict[str, Any]) -> list[dict[str, str]]:
    changes = {
        item.get("current_ref"): item.get("kind") for item in payload.get("changes", ()) if isinstance(item, dict)
    }
    result = []
    for claim in payload.get("claims", ())[:5]:
        if not isinstance(claim, dict):
            continue
        fields = claim.get("fields") or {}
        if not isinstance(fields, dict):
            continue
        item = {key: _clean(fields[key], limit=80) for key in _NEWS_LABELS if fields.get(key) is not None}
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
    source: dict[str, Any] = {
        key: _clean(source_fact[key], limit=80) for key in _NEWS_LABELS if source_fact.get(key) is not None
    }
    if trigger_kind == "catalyst":
        source["claims"] = _typed_claims(source_fact)
        if source_fact.get("text"):
            source["text"] = _clean(source_fact["text"], limit=1_200)
    elif trigger_kind == "oi":
        for key in ("oi_change_bps", "measurement_definition"):
            if source_fact.get(key) is not None:
                source[key] = _clean(source_fact[key], limit=120)
    context: list[dict[str, object]] = []
    for fact in recent_context[:5]:
        payload = fact["payload"]
        item: dict[str, object] = {
            "kind": fact["kind"],
            "claims": _typed_claims(payload),
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
        context.append(item)
    model_input = {
        "view": VIEW_VERSION,
        "trigger": trigger_kind,
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
    )
