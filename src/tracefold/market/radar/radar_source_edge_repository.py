from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from psycopg.types.json import Jsonb

from tracefold.market.radar.constants import (
    TOKEN_RADAR_DEFAULT_VENUE,
    TOKEN_RADAR_PROJECTION_VERSION,
    TOKEN_RADAR_RESOLVER_POLICY_VERSION,
    WINDOW_MS,
)
from tracefold.platform.postgres.postgres_client import require_transaction
from tracefold.platform.postgres.projection_frontier import (
    RADAR_FRONTIER,
    ProjectionFrontierRepository,
)
from tracefold.platform.postgres.write_contract import mutation_count
from tracefold.platform.validation import require_positive_int

_MAX_ANALYSIS_LOOKBACK_MS = 48 * 60 * 60 * 1000
_BASELINE_WINDOW_COUNT = 7
_EVENT_RESOLUTION_CAP = 100
_DEADLINE_MS = {
    "5m": 10_000,
    "1h": 60_000,
    "4h": 60_000,
    "24h": 60_000,
}


class RadarSourceEdgeRepository:
    """Incremental source-edge ownership for Token Radar material facts."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.frontiers = ProjectionFrontierRepository(conn)

    def sync_event(self, *, event_id: str, now_ms: int) -> int:
        """Synchronize only one material event's current resolution edges."""

        require_transaction(self.conn, operation="radar_source_edge_sync_event")
        event_key = _required_text(event_id, "event_id")
        material_at_ms = int(now_ms)
        rows = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT DISTINCT ON (
                  resolution.target_type,
                  resolution.target_id
                )
                  event.event_id,
                  event.received_at_ms,
                  intent.intent_id,
                  resolution.resolution_id,
                  resolution.target_type,
                  resolution.target_id,
                  resolution.pricefeed_id,
                  resolution.resolution_status
                FROM events event
                JOIN token_intents intent
                  ON intent.event_id = event.event_id
                JOIN token_intent_resolutions resolution
                  ON resolution.intent_id = intent.intent_id
                 AND resolution.event_id = event.event_id
                WHERE event.event_id = %s
                  AND resolution.is_current = true
                  AND (
                    resolution.target_type IN ('Asset', 'CexToken')
                    OR (
                      resolution.target_type = 'MarketInstrument'
                      AND resolution.resolution_status = 'NON_CRYPTO'
                      AND resolution.resolver_policy_version = %s
                      AND resolution.reason_codes_json
                            @> '["CONFIRMED_US_EQUITY"]'::jsonb
                    )
                  )
                  AND resolution.target_id IS NOT NULL
                ORDER BY
                  resolution.target_type,
                  resolution.target_id,
                  intent.intent_id,
                  resolution.resolution_id
                LIMIT %s
                """,
                (
                    event_key,
                    TOKEN_RADAR_RESOLVER_POLICY_VERSION,
                    _EVENT_RESOLUTION_CAP + 1,
                ),
            ).fetchall()
        ]
        if len(rows) > _EVENT_RESOLUTION_CAP:
            raise RuntimeError("radar_event_resolution_shard_oversized")

        desired = _desired_event_edges(rows, now_ms=material_at_ms)
        existing = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT *
                FROM radar_source_edges
                WHERE source_kind = 'event'
                  AND source_id = %s
                ORDER BY target_type, target_id, window_key, venue
                """,
                (event_key,),
            ).fetchall()
        ]
        existing_by_key = {_edge_key(row): row for row in existing}
        desired_by_key = {_edge_key(row): row for row in desired}
        changed_frontiers: dict[tuple[str, str, str, str], list[str]] = {}
        mutations = 0

        obsolete = sorted(set(existing_by_key) - set(desired_by_key))
        if obsolete:
            cursor = self.conn.execute(
                """
                WITH keys(
                  target_type, target_id, window_key, venue, source_kind, source_id
                ) AS (
                  SELECT *
                  FROM unnest(
                    %s::text[], %s::text[], %s::text[],
                    %s::text[], %s::text[], %s::text[]
                  )
                )
                DELETE FROM radar_source_edges edge
                USING keys
                WHERE edge.target_type = keys.target_type
                  AND edge.target_id = keys.target_id
                  AND edge.window_key = keys.window_key
                  AND edge.venue = keys.venue
                  AND edge.source_kind = keys.source_kind
                  AND edge.source_id = keys.source_id
                """,
                tuple([key[index] for key in obsolete] for index in range(6)),
            )
            mutations += mutation_count(
                cursor,
                error_code="radar_source_edge_delete_count_invalid",
            )
            for key in obsolete:
                _record_frontier_change(
                    changed_frontiers,
                    key[:4],
                    f"deleted:{existing_by_key[key]['input_fingerprint']}",
                )

        for key, row in desired_by_key.items():
            cursor = self.conn.execute(
                """
                INSERT INTO radar_source_edges(
                  target_type, target_id, window_key, venue,
                  source_kind, source_id, observed_at_ms, expires_at_ms,
                  input_fingerprint, payload_json, updated_at_ms
                )
                VALUES (
                  %(target_type)s, %(target_id)s, %(window_key)s, %(venue)s,
                  %(source_kind)s, %(source_id)s, %(observed_at_ms)s,
                  %(expires_at_ms)s, %(input_fingerprint)s, %(payload_json)s,
                  %(updated_at_ms)s
                )
                ON CONFLICT(
                  target_type, target_id, window_key, venue, source_kind, source_id
                ) DO UPDATE SET
                  observed_at_ms = EXCLUDED.observed_at_ms,
                  expires_at_ms = EXCLUDED.expires_at_ms,
                  input_fingerprint = EXCLUDED.input_fingerprint,
                  payload_json = EXCLUDED.payload_json,
                  updated_at_ms = EXCLUDED.updated_at_ms
                WHERE (
                  radar_source_edges.observed_at_ms,
                  radar_source_edges.expires_at_ms,
                  radar_source_edges.input_fingerprint,
                  radar_source_edges.payload_json
                ) IS DISTINCT FROM (
                  EXCLUDED.observed_at_ms,
                  EXCLUDED.expires_at_ms,
                  EXCLUDED.input_fingerprint,
                  EXCLUDED.payload_json
                )
                """,
                {**row, "payload_json": Jsonb(row["payload_json"])},
            )
            changed = mutation_count(
                cursor,
                error_code="radar_source_edge_upsert_count_invalid",
            )
            mutations += changed
            if changed:
                _record_frontier_change(
                    changed_frontiers,
                    key[:4],
                    str(row["input_fingerprint"]),
                )

        self._mark_changed_frontiers(
            changed_frontiers,
            dirty_at_ms=material_at_ms,
        )
        return mutations

    def mark_market_targets(
        self,
        targets: Sequence[tuple[str, str]],
        *,
        now_ms: int,
        input_fingerprint: str,
    ) -> int:
        """Dirty one target-window closure after monotonic market-current CAS."""

        require_transaction(self.conn, operation="radar_market_frontier_mark_dirty")
        marker = _required_text(input_fingerprint, "input_fingerprint")
        changed = 0
        for target_type, target_id in sorted(set(targets)):
            for window in WINDOW_MS:
                changed += self.frontiers.mark_dirty(
                    RADAR_FRONTIER,
                    key={
                        "target_type": _required_text(target_type, "target_type"),
                        "target_id": _required_text(target_id, "target_id"),
                        "window_key": window,
                        "venue": TOKEN_RADAR_DEFAULT_VENUE,
                    },
                    dirty_at_ms=int(now_ms),
                    deadline_at_ms=int(now_ms) + _DEADLINE_MS[window],
                    input_fingerprint=_fingerprint(
                        {
                            "kind": "market_current",
                            "marker": marker,
                            "target_type": target_type,
                            "target_id": target_id,
                            "window": window,
                        }
                    ),
                    version=TOKEN_RADAR_PROJECTION_VERSION,
                )
        return changed

    def expire_due(self, *, now_ms: int, limit: int) -> int:
        """Delete one deterministic expiry batch and dirty only its closures."""

        require_transaction(self.conn, operation="radar_source_edge_expire_due")
        row_limit = require_positive_int(
            limit,
            error_code="radar_source_edge_expiry_limit_required",
        )
        rows = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT *
                FROM radar_source_edges
                WHERE expires_at_ms <= %s
                ORDER BY
                  expires_at_ms,
                  target_type,
                  target_id,
                  window_key,
                  venue,
                  source_kind,
                  source_id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (int(now_ms), row_limit),
            ).fetchall()
        ]
        if not rows:
            return 0
        keys = [_edge_key(row) for row in rows]
        cursor = self.conn.execute(
            """
            WITH keys(
              target_type, target_id, window_key, venue, source_kind, source_id
            ) AS (
              SELECT *
              FROM unnest(
                %s::text[], %s::text[], %s::text[],
                %s::text[], %s::text[], %s::text[]
              )
            )
            DELETE FROM radar_source_edges edge
            USING keys
            WHERE edge.target_type = keys.target_type
              AND edge.target_id = keys.target_id
              AND edge.window_key = keys.window_key
              AND edge.venue = keys.venue
              AND edge.source_kind = keys.source_kind
              AND edge.source_id = keys.source_id
            """,
            tuple([key[index] for key in keys] for index in range(6)),
        )
        deleted = mutation_count(
            cursor,
            error_code="radar_source_edge_expiry_count_invalid",
        )
        if deleted != len(rows):
            raise RuntimeError("radar_source_edge_expiry_cas_mismatch")
        changed_frontiers: dict[tuple[str, str, str, str], list[str]] = {}
        for row in rows:
            _record_frontier_change(
                changed_frontiers,
                _edge_key(row)[:4],
                f"expired:{row['input_fingerprint']}",
            )
        self._mark_changed_frontiers(
            changed_frontiers,
            dirty_at_ms=int(now_ms),
        )
        return deleted

    def _mark_changed_frontiers(
        self,
        changes: Mapping[tuple[str, str, str, str], Sequence[str]],
        *,
        dirty_at_ms: int,
    ) -> None:
        for target_type, target_id, window, venue in sorted(changes):
            self.frontiers.mark_dirty(
                RADAR_FRONTIER,
                key={
                    "target_type": target_type,
                    "target_id": target_id,
                    "window_key": window,
                    "venue": venue,
                },
                dirty_at_ms=int(dirty_at_ms),
                deadline_at_ms=int(dirty_at_ms) + _DEADLINE_MS[window],
                input_fingerprint=_fingerprint(
                    {
                        "target_type": target_type,
                        "target_id": target_id,
                        "window": window,
                        "venue": venue,
                        "changes": sorted(changes[(target_type, target_id, window, venue)]),
                    }
                ),
                version=TOKEN_RADAR_PROJECTION_VERSION,
            )


def _desired_event_edges(
    rows: Sequence[Mapping[str, Any]],
    *,
    now_ms: int,
) -> list[dict[str, Any]]:
    desired: list[dict[str, Any]] = []
    for source in rows:
        observed_at_ms = int(source["received_at_ms"])
        payload = {
            "intent_id": str(source["intent_id"]),
            "event_id": str(source["event_id"]),
            "resolution_id": str(source["resolution_id"]),
            "target_type": str(source["target_type"]),
            "target_id": str(source["target_id"]),
            "pricefeed_id": source.get("pricefeed_id"),
            "resolution_status": str(source["resolution_status"]),
        }
        for window, window_ms in WINDOW_MS.items():
            expires_at_ms = observed_at_ms + min(
                _BASELINE_WINDOW_COUNT * window_ms,
                _MAX_ANALYSIS_LOOKBACK_MS,
            )
            if expires_at_ms <= int(now_ms):
                continue
            identity = {
                "target_type": str(source["target_type"]),
                "target_id": str(source["target_id"]),
                "window_key": window,
                "venue": TOKEN_RADAR_DEFAULT_VENUE,
                "source_kind": "event",
                "source_id": str(source["event_id"]),
            }
            desired.append(
                {
                    **identity,
                    "observed_at_ms": observed_at_ms,
                    "expires_at_ms": expires_at_ms,
                    "input_fingerprint": _fingerprint(
                        {
                            **identity,
                            "observed_at_ms": observed_at_ms,
                            "expires_at_ms": expires_at_ms,
                            "payload": payload,
                        }
                    ),
                    "payload_json": payload,
                    "updated_at_ms": int(now_ms),
                }
            )
    return desired


def _edge_key(row: Mapping[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        str(row["target_type"]),
        str(row["target_id"]),
        str(row["window_key"]),
        str(row["venue"]),
        str(row["source_kind"]),
        str(row["source_id"]),
    )


def _record_frontier_change(
    changes: dict[tuple[str, str, str, str], list[str]],
    key: tuple[str, str, str, str],
    marker: str,
) -> None:
    changes.setdefault(key, []).append(marker)


def _fingerprint(value: Mapping[str, Any]) -> str:
    serialized = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"radar_source_edge_{field}_required")
    return text


__all__ = ["RadarSourceEdgeRepository"]
