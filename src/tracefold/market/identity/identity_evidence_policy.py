from __future__ import annotations

from typing import Any

EVIDENCE_MANUAL_IDENTITY_REPAIR = "manual_identity_repair"
EVIDENCE_GMGN_OPENAPI_EXACT = "gmgn_openapi_exact"
EVIDENCE_GMGN_PAYLOAD_EXACT = "gmgn_payload_exact"
EVIDENCE_BINANCE_CEX_INSTRUMENT = "binance_cex_instrument"
EVIDENCE_TWEET_CONTRACT_MENTION = "tweet_contract_mention"

CONFIDENCE_MANUAL = "manual"
CONFIDENCE_PROVIDER_EXACT = "provider_exact"
CONFIDENCE_MENTION_ONLY = "mention_only"
CONFIDENCE_UNKNOWN = "unknown"

_EVIDENCE_ORDER = (
    EVIDENCE_MANUAL_IDENTITY_REPAIR,
    EVIDENCE_GMGN_OPENAPI_EXACT,
    EVIDENCE_GMGN_PAYLOAD_EXACT,
    EVIDENCE_BINANCE_CEX_INSTRUMENT,
    EVIDENCE_TWEET_CONTRACT_MENTION,
)

_CONFIDENCE_BY_KIND = {
    EVIDENCE_MANUAL_IDENTITY_REPAIR: CONFIDENCE_MANUAL,
    EVIDENCE_GMGN_OPENAPI_EXACT: CONFIDENCE_PROVIDER_EXACT,
    EVIDENCE_GMGN_PAYLOAD_EXACT: CONFIDENCE_PROVIDER_EXACT,
    EVIDENCE_BINANCE_CEX_INSTRUMENT: CONFIDENCE_PROVIDER_EXACT,
    EVIDENCE_TWEET_CONTRACT_MENTION: CONFIDENCE_MENTION_ONLY,
}


def select_current_identity(
    *,
    asset_id: str,
    evidence_rows: list[dict[str, Any]],
    now_ms: int,
) -> dict[str, Any]:
    selected = _select_evidence(evidence_rows)
    if selected is None:
        return {
            "asset_id": asset_id,
            "canonical_symbol": None,
            "canonical_name": None,
            "decimals": None,
            "identity_confidence": CONFIDENCE_UNKNOWN,
            "selected_evidence_id": None,
            "selection_reason_codes": ["NO_IDENTITY_EVIDENCE"],
            "conflict_count": 0,
            "verified_at_ms": int(now_ms),
            "updated_at_ms": int(now_ms),
        }

    reason_codes = [_selection_reason(str(selected.get("evidence_kind") or ""))]
    conflict_count = _conflict_count(selected=selected, evidence_rows=evidence_rows)
    if conflict_count:
        reason_codes.append("CONFLICTING_IDENTITY_EVIDENCE")
    if _mention_not_canonical(selected=selected, evidence_rows=evidence_rows):
        reason_codes.append("MENTION_NOT_CANONICAL")
    if _multiple_mention_aliases(evidence_rows):
        reason_codes.append("MULTIPLE_MENTION_ALIASES")

    return {
        "asset_id": asset_id,
        "canonical_symbol": _symbol(selected.get("symbol")),
        "canonical_name": _string(selected.get("name")),
        "decimals": selected.get("decimals"),
        "identity_confidence": _CONFIDENCE_BY_KIND.get(str(selected.get("evidence_kind") or ""), CONFIDENCE_UNKNOWN),
        "selected_evidence_id": selected.get("evidence_id"),
        "selection_reason_codes": reason_codes,
        "conflict_count": conflict_count,
        "verified_at_ms": int(now_ms),
        "updated_at_ms": int(now_ms),
    }


def _select_evidence(evidence_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows = [row for row in evidence_rows if _has_identity_value(row)]
    for evidence_kind in _EVIDENCE_ORDER:
        candidates = [row for row in rows if str(row.get("evidence_kind") or "") == evidence_kind]
        if candidates:
            return max(candidates, key=_evidence_sort_key)
    return None


def _evidence_sort_key(row: dict[str, Any]) -> tuple[int, str]:
    return (int(row.get("observed_at_ms") or row.get("created_at_ms") or 0), str(row.get("evidence_id") or ""))


def _has_identity_value(row: dict[str, Any]) -> bool:
    return _symbol(row.get("symbol")) is not None or _string(row.get("name")) is not None


def _selection_reason(evidence_kind: str) -> str:
    if evidence_kind == EVIDENCE_MANUAL_IDENTITY_REPAIR:
        return "SELECTED_MANUAL_REPAIR"
    if _CONFIDENCE_BY_KIND.get(evidence_kind) == CONFIDENCE_PROVIDER_EXACT:
        return "SELECTED_PROVIDER_EXACT"
    if evidence_kind == EVIDENCE_TWEET_CONTRACT_MENTION:
        return "MENTION_ONLY_IDENTITY"
    return "SELECTED_UNKNOWN_IDENTITY"


def _conflict_count(*, selected: dict[str, Any], evidence_rows: list[dict[str, Any]]) -> int:
    selected_symbol = _symbol(selected.get("symbol"))
    selected_name = _string(selected.get("name"))
    conflicts = 0
    for row in evidence_rows:
        if row.get("evidence_id") == selected.get("evidence_id"):
            continue
        symbol = _symbol(row.get("symbol"))
        name = _string(row.get("name"))
        if symbol is not None and selected_symbol is not None and symbol != selected_symbol:
            conflicts += 1
            continue
        if name is not None and selected_name is not None and name != selected_name:
            conflicts += 1
    return conflicts


def _mention_not_canonical(*, selected: dict[str, Any], evidence_rows: list[dict[str, Any]]) -> bool:
    selected_symbol = _symbol(selected.get("symbol"))
    if selected_symbol is None:
        return False
    for row in evidence_rows:
        if str(row.get("evidence_kind") or "") != EVIDENCE_TWEET_CONTRACT_MENTION:
            continue
        mention_symbol = _symbol(row.get("symbol"))
        if mention_symbol is not None and mention_symbol != selected_symbol:
            return True
    return False


def _multiple_mention_aliases(evidence_rows: list[dict[str, Any]]) -> bool:
    aliases = {
        _symbol(row.get("symbol"))
        for row in evidence_rows
        if str(row.get("evidence_kind") or "") == EVIDENCE_TWEET_CONTRACT_MENTION
    }
    aliases.discard(None)
    return len(aliases) > 1


def _symbol(value: Any) -> str | None:
    text = str(value or "").strip().lstrip("$")
    if not text:
        return None
    return text.upper() if text.isascii() else text


def _string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
