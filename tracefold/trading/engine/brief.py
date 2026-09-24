"""Render exactly the frozen evidence and finite menu shown to the analyst."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .contracts import Candidate
from .policy import is_citable_evidence


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AnalystBrief:
    text: str
    sha: str
    candidate_menu_sha: str
    evidence_catalog: dict[str, dict[str, Any]]


def build_brief(
    *,
    target_asset_id: str,
    instrument_semantics_digest: str,
    source_fact: dict[str, Any],
    source_history: tuple[dict[str, Any], ...],
    evidence: dict[str, dict[str, Any]],
    features: dict[str, Any],
    candidates: tuple[Candidate, ...],
    trigger_context: dict[str, Any] | None = None,
    typed_evidence: dict[str, Any] | None = None,
) -> AnalystBrief:
    if any(
        candidate.asset_id != target_asset_id or candidate.instrument_semantics_digest != instrument_semantics_digest
        for candidate in candidates
    ):
        raise ValueError("brief_candidate_target_mismatch")
    menu = [candidate.model_dump(mode="json") for candidate in candidates]
    menu_sha = sha256(canonical_json(menu))
    payload = {
        "brief_version": "trade_brief_v3",
        "target_asset_id": target_asset_id,
        "instrument_semantics_digest": instrument_semantics_digest,
        "source_fact": source_fact,
        "same_asset_source_history": source_history,
        "evidence": evidence,
        "citable_evidence_ids": sorted(ref for ref, item in evidence.items() if is_citable_evidence(item)),
        "features": features,
        "candidate_menu": menu,
        "candidate_menu_sha": menu_sha,
        "trigger_context": trigger_context,
        "typed_evidence": typed_evidence,
    }
    text = canonical_json(payload)
    return AnalystBrief(text, sha256(text), menu_sha, evidence)
