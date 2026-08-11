from __future__ import annotations

import hashlib
import json
from typing import Any

from psycopg.types.json import Jsonb

from tracefold.market.identity.chain_identity import canonical_chain_id
from tracefold.market.identity.identity_evidence_policy import (
    CONFIDENCE_UNKNOWN,
    select_current_identity,
)
from tracefold.platform.postgres.write_contract import returning_mutation_count


class IdentityEvidenceRepository:
    def __init__(self, conn: Any):
        self.conn = conn

    def upsert_identity_evidence(
        self,
        *,
        asset_id: str,
        evidence_kind: str,
        provider: str,
        lookup_mode: str,
        chain_id: str,
        address: str,
        observed_at_ms: int,
        symbol: str | None = None,
        name: str | None = None,
        decimals: int | None = None,
        confidence: str = CONFIDENCE_UNKNOWN,
        source_event_id: str | None = None,
        source_intent_id: str | None = None,
        source_resolution_id: str | None = None,
        raw_payload: dict[str, Any] | None = None,
        evidence_id: str | None = None,
    ) -> dict[str, Any]:
        payload = raw_payload or {}
        row_id = evidence_id or _evidence_id(
            asset_id=asset_id,
            evidence_kind=evidence_kind,
            provider=provider,
            lookup_mode=lookup_mode,
            symbol=symbol,
            name=name,
            source_event_id=source_event_id,
            source_intent_id=source_intent_id,
            source_resolution_id=source_resolution_id,
            raw_payload=payload,
        )
        self.conn.execute(
            """
            INSERT INTO asset_identity_evidence(
              evidence_id, asset_id, evidence_kind, provider, lookup_mode, chain_id, address,
              symbol, name, decimals, confidence, source_event_id, source_intent_id,
              source_resolution_id, raw_payload_json, observed_at_ms, created_at_ms, updated_at_ms
            )
            VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT(evidence_id) DO UPDATE SET
              symbol = excluded.symbol,
              name = excluded.name,
              decimals = excluded.decimals,
              confidence = excluded.confidence,
              raw_payload_json = excluded.raw_payload_json,
              observed_at_ms = excluded.observed_at_ms,
              updated_at_ms = excluded.updated_at_ms
            """,
            (
                row_id,
                asset_id,
                evidence_kind,
                provider,
                lookup_mode,
                canonical_chain_id(chain_id),
                _address(address),
                _symbol(symbol) if symbol else None,
                name,
                decimals,
                confidence,
                source_event_id,
                source_intent_id,
                source_resolution_id,
                Jsonb(payload),
                int(observed_at_ms),
                int(observed_at_ms),
                int(observed_at_ms),
            ),
        )
        return self._row_by_id("asset_identity_evidence", "evidence_id", row_id) or {
            "evidence_id": row_id,
            "asset_id": asset_id,
            "evidence_kind": evidence_kind,
            "provider": provider,
            "lookup_mode": lookup_mode,
            "chain_id": canonical_chain_id(chain_id),
            "address": _address(address),
            "symbol": _symbol(symbol) if symbol else None,
            "name": name,
            "decimals": decimals,
            "confidence": confidence,
            "observed_at_ms": int(observed_at_ms),
        }

    def list_identity_evidence(self, asset_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM asset_identity_evidence
            WHERE asset_id = %s
            ORDER BY observed_at_ms DESC, evidence_id
            """,
            (asset_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def recompute_current_identity(self, asset_id: str, *, now_ms: int) -> dict[str, Any]:
        current = select_current_identity(
            asset_id=asset_id,
            evidence_rows=self.list_identity_evidence(asset_id),
            now_ms=now_ms,
        )
        changed = self._upsert_current_identity(current)
        return {**current, "rows_written": int(changed)}

    def current_identity(self, asset_id: str) -> dict[str, Any] | None:
        return self._row_by_id("asset_identity_current", "asset_id", asset_id)

    def _upsert_current_identity(self, current: dict[str, Any]) -> bool:
        cursor = self.conn.execute(
            """
            INSERT INTO asset_identity_current(
              asset_id, canonical_symbol, canonical_name, decimals, identity_confidence,
              selected_evidence_id, selection_reason_codes_json, conflict_count, verified_at_ms, updated_at_ms
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(asset_id) DO UPDATE SET
              canonical_symbol = excluded.canonical_symbol,
              canonical_name = excluded.canonical_name,
              decimals = excluded.decimals,
              identity_confidence = excluded.identity_confidence,
              selected_evidence_id = excluded.selected_evidence_id,
              selection_reason_codes_json = excluded.selection_reason_codes_json,
              conflict_count = excluded.conflict_count,
              verified_at_ms = excluded.verified_at_ms,
              updated_at_ms = excluded.updated_at_ms
            WHERE asset_identity_current.canonical_symbol IS DISTINCT FROM excluded.canonical_symbol
               OR asset_identity_current.canonical_name IS DISTINCT FROM excluded.canonical_name
               OR asset_identity_current.decimals IS DISTINCT FROM excluded.decimals
               OR asset_identity_current.identity_confidence IS DISTINCT FROM excluded.identity_confidence
               OR asset_identity_current.selected_evidence_id IS DISTINCT FROM excluded.selected_evidence_id
               OR asset_identity_current.selection_reason_codes_json
                  IS DISTINCT FROM excluded.selection_reason_codes_json
               OR asset_identity_current.conflict_count IS DISTINCT FROM excluded.conflict_count
            RETURNING true AS changed
            """,
            (
                current["asset_id"],
                current["canonical_symbol"],
                current["canonical_name"],
                current["decimals"],
                current["identity_confidence"],
                current["selected_evidence_id"],
                Jsonb(current["selection_reason_codes"]),
                current["conflict_count"],
                current["verified_at_ms"],
                current["updated_at_ms"],
            ),
        )
        row = cursor.fetchone()
        return _single_returning_changed(cursor, row)

    def _row_by_id(self, table: str, key: str, value: str) -> dict[str, Any] | None:
        row = self.conn.execute(f"SELECT * FROM {table} WHERE {key} = %s", (value,)).fetchone()
        return dict(row) if row else None


def _evidence_id(
    *,
    asset_id: str,
    evidence_kind: str,
    provider: str,
    lookup_mode: str,
    symbol: str | None,
    name: str | None,
    source_event_id: str | None,
    source_intent_id: str | None,
    source_resolution_id: str | None,
    raw_payload: dict[str, Any],
) -> str:
    payload = json.dumps(raw_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(
        "|".join(
            [
                asset_id,
                evidence_kind,
                provider,
                lookup_mode,
                _symbol(symbol) or "",
                str(name or "").strip(),
                str(source_event_id or ""),
                str(source_intent_id or ""),
                str(source_resolution_id or ""),
                payload,
            ]
        ).encode("utf-8")
    ).hexdigest()
    return f"asset-identity-evidence:{digest}"


def _single_returning_changed(cursor: Any, row: Any | None) -> bool:
    returning_mutation_count(cursor, row, error_code="identity_evidence_repository_rowcount_invalid")
    return row is not None and bool(row.get("changed", True))


def _address(value: str) -> str:
    text = str(value or "").strip()
    return text.lower() if text.startswith(("0x", "0X")) else text


def _symbol(value: Any) -> str | None:
    text = str(value or "").strip().lstrip("$")
    if not text:
        return None
    return text.upper() if text.isascii() else text
