"""Render exactly the frozen evidence and finite menu shown to the analyst."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .plans import EntryPlan
from .policy import is_citable_evidence


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AnalystBrief:
    text: str
    sha: str
    plan_menu_sha: str
    evidence_catalog: dict[str, dict[str, Any]]


def build_brief(
    *,
    target_asset_id: str,
    instrument_semantics_digest: str,
    source_fact: dict[str, Any],
    source_history: tuple[dict[str, Any], ...],
    evidence: dict[str, dict[str, Any]],
    plans: tuple[EntryPlan, ...],
    trigger_context: dict[str, Any] | None = None,
    source_amendments: tuple[dict[str, Any], ...] = (),
) -> AnalystBrief:
    """`source_amendments` are recorded News corrections and evidence changes to the source's own claims.

    They inform the analysis; they grant or revoke nothing. Entry refusal on a retired claim is the
    storage-owned last check before submission, not a brief field.
    """
    if any(
        plan.asset_id != target_asset_id or plan.instrument_semantics_digest != instrument_semantics_digest
        for plan in plans
    ):
        raise ValueError("brief_plan_target_mismatch")
    catalog = dict(evidence)
    for item in source_history:
        material = item.get("payload") or {}
        raw = (item.get("trigger_id"), item.get("source_revision"), material)
        ref = "event:" + sha256(canonical_json(raw))
        text = material.get("text") if material.get("kind") == "catalyst_delta" else None
        text = text if isinstance(text, str) and text.strip() else canonical_json(material)
        visible = int(item["first_visible_at_ms"])
        catalog.setdefault(
            ref,
            {
                "status": "ok",
                "source_ref": evidence["source"].get("source_ref"),
                "source_revision": item.get("source_revision"),
                "values": {"text": text[:2_048]},
                "unit_definition": {"text": "source_text"},
                "event_at_ms": int(item.get("source_observed_at_ms") or visible),
                "received_at_ms": visible,
                "knowledge_cutoff_ms": evidence["source"].get("knowledge_cutoff_ms"),
                "truncated": len(text) > 2_048,
            },
        )
    for item in source_amendments:
        ref = "amendment:" + sha256(canonical_json(item))
        visible = int(item["received_at_ms"])
        payload = item.get("payload") or {}
        text = payload.get("text") if isinstance(payload, dict) else None
        text = text if isinstance(text, str) and text.strip() else canonical_json(payload)
        catalog.setdefault(
            ref,
            {
                "status": "ok",
                "source_ref": evidence["source"].get("source_ref"),
                "update_id": item.get("update_id"),
                "source_revision": item.get("content_revision"),
                "affected_claim_refs": item.get("affected_claim_refs"),
                "retired_claim_refs": item.get("retired_claim_refs"),
                "values": {"text": text[:2_048]},
                "unit_definition": {"text": "source_amendment"},
                "event_at_ms": visible,
                "received_at_ms": visible,
                "knowledge_cutoff_ms": evidence["source"].get("knowledge_cutoff_ms"),
                "truncated": len(text) > 2_048,
            },
        )
    menu = [plan.model_dump(mode="json") for plan in plans]
    menu_sha = sha256(canonical_json(menu))
    payload = {
        "brief_version": "trade_brief_v5",
        "target_asset_id": target_asset_id,
        "instrument_semantics_digest": instrument_semantics_digest,
        "source_context": {
            key: source_fact[key] for key in ("kind", "source_venue", "source_recorded_at_ms") if key in source_fact
        },
        "evidence": catalog,
        "citable_evidence_ids": sorted(ref for ref, item in catalog.items() if is_citable_evidence(item)),
        "plan_menu": menu,
        "plan_menu_sha": menu_sha,
        "trigger_context": trigger_context,
    }
    text = canonical_json(payload)
    return AnalystBrief(text, sha256(text), menu_sha, catalog)
