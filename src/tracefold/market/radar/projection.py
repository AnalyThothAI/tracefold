from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID, uuid4

from tracefold.market.radar.constants import (
    TOKEN_RADAR_DEFAULT_VENUE,
    TOKEN_RADAR_PROJECTION_VERSION,
    TOKEN_RADAR_VENUES,
    WINDOW_MS,
)
from tracefold.market.radar.token_radar_projector import (
    build_token_radar_current_closure,
    compute_token_radar_target_projection,
    rank_token_radar_closure,
    token_radar_venue_for_rank_input,
)
from tracefold.market.radar.token_radar_rank_source_query import (
    TokenRadarFeatureSourceRequest,
)
from tracefold.market.radar.token_radar_repository import stable_generation_id
from tracefold.platform.postgres.projection_frontier import RADAR_FRONTIER

_CLAIM_LEASE_MS = 5_000
_CLAIM_TRANSACTION_TIMEOUT_SECONDS = 0.5
_PUBLISH_TRANSACTION_TIMEOUT_SECONDS = 1.0
_STEADY_STATEMENT_TIMEOUT_SECONDS = 3.0
_MAINTENANCE_STATEMENT_TIMEOUT_SECONDS = 120.0
_INPUT_ROW_CAP = 10_000
_INPUT_BYTE_CAP = 4 * 1024 * 1024
_OUTPUT_BYTE_CAP = 1 * 1024 * 1024
_EXPIRY_BATCH_SIZE = 100
_PROFILE_DEADLINE_MS = 30_000
_RANK_LIMIT = 100
_DEADLINE_MS = {
    "5m": 10_000,
    "1h": 60_000,
    "4h": 60_000,
    "24h": 60_000,
}
_MAINTENANCE_EVENT_BATCH_SIZE = 1_000
_MAINTENANCE_SHARD_CAP = 1_000_000


class RadarShardOversized(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RadarProjectionClaim:
    target_type: str
    target_id: str
    window: str
    venue: str
    runtime_id: str
    input_fingerprint: str
    projection_version: str
    deadline_at_ms: int

    @property
    def key(self) -> dict[str, str]:
        return {
            "target_type": self.target_type,
            "target_id": self.target_id,
            "window_key": self.window,
            "venue": self.venue,
        }


class RadarProjectionService:
    """Short claim/load/publish transactions for one target-window reducer."""

    def __init__(
        self,
        *,
        db: Any,
        worker_name: str = "steady_projection_coordinator",
    ) -> None:
        self.db = db
        self.worker_name = worker_name

    def next_due(self, *, now_ms: int) -> dict[str, Any] | None:
        with self._session(
            transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
        ) as repos, repos.transaction():
            repos.radar_source_edges.expire_due(
                now_ms=now_ms,
                limit=_EXPIRY_BATCH_SIZE,
            )
            return cast(
                dict[str, Any] | None,
                repos.projection_frontiers.next_due(
                    RADAR_FRONTIER,
                    now_ms=now_ms,
                ),
            )

    def claim(
        self,
        *,
        key: dict[str, str],
        runtime_id: str,
        now_ms: int,
    ) -> RadarProjectionClaim | None:
        with self._session(
            transaction_timeout_seconds=_CLAIM_TRANSACTION_TIMEOUT_SECONDS,
        ) as repos, repos.transaction():
            row = repos.projection_frontiers.claim(
                RADAR_FRONTIER,
                key=key,
                runtime_id=runtime_id,
                now_ms=now_ms,
                lease_ms=_CLAIM_LEASE_MS,
            )
        if row is None:
            return None
        return RadarProjectionClaim(
            target_type=str(row["target_type"]),
            target_id=str(row["target_id"]),
            window=str(row["window_key"]),
            venue=str(row["venue"]),
            runtime_id=str(UUID(str(runtime_id))),
            input_fingerprint=str(row["input_fingerprint"]),
            projection_version=str(row["projection_version"]),
            deadline_at_ms=int(row["deadline_at_ms"]),
        )

    def load_target(
        self,
        claim: RadarProjectionClaim,
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        window_ms = WINDOW_MS[claim.window]
        request = TokenRadarFeatureSourceRequest(
            request_key="target",
            target_type_key=claim.target_type,
            identity_id=claim.target_id,
            window=claim.window,
            analysis_since_ms=max(
                int(now_ms) - 7 * window_ms,
                int(now_ms) - 48 * 60 * 60 * 1000,
            ),
            score_since_ms=int(now_ms) - window_ms,
            now_ms=int(now_ms),
        )
        try:
            with self._session() as repos:
                source_rows = repos.radar_projection_sources.load_rows_for_requests(
                    (request,),
                    row_cap=_INPUT_ROW_CAP,
                )["target"]
                compact_inputs = repos.token_radar.list_compact_rank_inputs_for_rank_set(
                    projection_version=TOKEN_RADAR_PROJECTION_VERSION,
                    window=claim.window,
                    min_latest_event_received_at_ms=int(now_ms) - window_ms,
                    row_cap=_INPUT_ROW_CAP,
                )
                old_features = [
                    dict(row)
                    for row in repos.conn.execute(
                        """
                        SELECT *
                        FROM token_radar_target_features
                        WHERE projection_version = %s
                          AND "window" = %s
                          AND target_type_key = %s
                          AND identity_id = %s
                        ORDER BY lane
                        LIMIT 3
                        """,
                        (
                            TOKEN_RADAR_PROJECTION_VERSION,
                            claim.window,
                            claim.target_type,
                            claim.target_id,
                        ),
                    ).fetchall()
                ]
        except RuntimeError as exc:
            if "shard_oversized" in str(exc):
                raise RadarShardOversized(str(exc)) from exc
            raise
        venues = {TOKEN_RADAR_DEFAULT_VENUE}
        for row in old_features:
            old_venue = token_radar_venue_for_rank_input(row)
            if old_venue in TOKEN_RADAR_VENUES:
                venues.add(old_venue)
        loaded = {
            "target_type": claim.target_type,
            "target_id": claim.target_id,
            "window": claim.window,
            "now_ms": int(now_ms),
            "source_rows": source_rows,
            "compact_inputs": compact_inputs,
            "old_venues": sorted(venues),
        }
        _require_bounded_input(loaded)
        return loaded

    def load_hydration(
        self,
        claim: RadarProjectionClaim,
        *,
        target_projection: dict[str, Any],
        ranked: dict[str, Any],
    ) -> list[dict[str, Any]]:
        feature = target_projection.get("feature")
        new_identity = (
            (
                str(feature["lane"]),
                claim.target_type,
                claim.target_id,
            )
            if isinstance(feature, dict)
            else None
        )
        identities = [
            tuple(str(part) for part in identity)
            for identity in ranked["selected_identities"]
            if tuple(str(part) for part in identity) != new_identity
        ]
        if len(identities) > 2 * _RANK_LIMIT:
            raise RadarShardOversized("radar_rank_hydration_shard_oversized")
        with self._session() as repos:
            rows = cast(
                list[dict[str, Any]],
                repos.token_radar.hydrate_rank_inputs_for_rank_set(
                    projection_version=TOKEN_RADAR_PROJECTION_VERSION,
                    window=claim.window,
                    identities=identities,
                ),
            )
        payload = {"rows": rows}
        _require_bounded_input(payload)
        return rows

    def publish(
        self,
        claim: RadarProjectionClaim,
        *,
        target_projection: dict[str, Any],
        ranked: dict[str, Any],
        closure: dict[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        _require_bounded_output(closure)
        if set(closure["rows_by_venue"]) != {claim.venue}:
            raise RuntimeError("radar_publish_cross_venue_shard_forbidden")
        with self._session(
            transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
        ) as repos, repos.transaction():
            frontier = repos.conn.execute(
                """
                SELECT status, claimed_by, input_fingerprint, projection_version
                FROM radar_projection_frontiers
                WHERE target_type = %s
                  AND target_id = %s
                  AND window_key = %s
                  AND venue = %s
                FOR UPDATE
                """,
                (
                    claim.target_type,
                    claim.target_id,
                    claim.window,
                    claim.venue,
                ),
            ).fetchone()
            if not _claim_still_current(frontier, claim):
                return {
                    "projection_status": "stale_snapshot",
                    "rows_written": 0,
                }

            feature = target_projection.get("feature")
            raw_projected = target_projection.get("projected")
            feature_writes = 0
            if isinstance(feature, dict) and isinstance(raw_projected, dict):
                feature_writes += repos.token_radar.upsert_target_feature(
                    projection_version=TOKEN_RADAR_PROJECTION_VERSION,
                    window=claim.window,
                    row=raw_projected,
                    computed_at_ms=int(now_ms),
                )
                opposite_lane = "attention" if str(feature["lane"]) == "resolved" else "resolved"
                feature_writes += repos.token_radar.delete_target_feature(
                    projection_version=TOKEN_RADAR_PROJECTION_VERSION,
                    window=claim.window,
                    lane=opposite_lane,
                    target_type_key=claim.target_type,
                    identity_id=claim.target_id,
                )
            else:
                for lane in ("resolved", "attention"):
                    feature_writes += repos.token_radar.delete_target_feature(
                        projection_version=TOKEN_RADAR_PROJECTION_VERSION,
                        window=claim.window,
                        lane=lane,
                        target_type_key=claim.target_type,
                        identity_id=claim.target_id,
                    )

            publication_writes = 0
            publication_status: dict[str, str] = {}
            for venue in sorted(closure["rows_by_venue"]):
                rows = [dict(row) for row in closure["rows_by_venue"][venue]]
                generation_id = stable_generation_id(
                    projection_version=TOKEN_RADAR_PROJECTION_VERSION,
                    window=claim.window,
                    venue=venue,
                    rows=rows,
                )
                source_frontier_ms = max(
                    (int(row.get("source_max_received_at_ms") or 0) for row in rows),
                    default=0,
                )
                result = repos.token_radar.publish_current_generation(
                    projection_version=TOKEN_RADAR_PROJECTION_VERSION,
                    window=claim.window,
                    venue=venue,
                    generation_id=generation_id,
                    published_at_ms=int(now_ms),
                    source_frontier_ms=source_frontier_ms,
                    rows=rows,
                    source_rows=int(ranked["source_rows_by_venue"].get(venue, 0)),
                    started_at_ms=int(now_ms),
                    finished_at_ms=int(now_ms),
                    on_current_changes=self._profile_frontier_callback(repos),
                )
                status = str(result["status"])
                if status not in {"published", "unchanged"}:
                    raise RuntimeError(f"radar_atomic_publication_failed:{venue}:{status}")
                publication_status[venue] = status
                publication_writes += int(result["rows_written"])

            self._mark_affected_venue_frontiers(
                repos,
                claim=claim,
                target_projection=target_projection,
                now_ms=int(now_ms),
            )
            if not repos.projection_frontiers.complete(
                RADAR_FRONTIER,
                key=claim.key,
                runtime_id=claim.runtime_id,
                input_fingerprint=claim.input_fingerprint,
                version=claim.projection_version,
                now_ms=int(now_ms),
            ):
                raise RuntimeError("radar_frontier_completion_cas_mismatch")
        rows_written = feature_writes + publication_writes
        return {
            "projection_status": ("published" if rows_written else "unchanged"),
            "rows_written": rows_written,
            "source_rows": int(target_projection["source_rows"]),
            "venues": publication_status,
            "target_type": claim.target_type,
            "target_id": claim.target_id,
            "window": claim.window,
        }

    @staticmethod
    def _mark_affected_venue_frontiers(
        repos: Any,
        *,
        claim: RadarProjectionClaim,
        target_projection: dict[str, Any],
        now_ms: int,
    ) -> None:
        if claim.venue != TOKEN_RADAR_DEFAULT_VENUE:
            return
        affected = {
            str(venue)
            for venue in target_projection["old_venues"]
        } | {str(target_projection["target_venue"])}
        affected.discard(TOKEN_RADAR_DEFAULT_VENUE)
        for venue in sorted(affected):
            repos.projection_frontiers.mark_dirty(
                RADAR_FRONTIER,
                key={
                    "target_type": claim.target_type,
                    "target_id": claim.target_id,
                    "window_key": claim.window,
                    "venue": venue,
                },
                dirty_at_ms=int(now_ms),
                deadline_at_ms=int(now_ms) + _DEADLINE_MS[claim.window],
                input_fingerprint=_fingerprint(
                    {
                        "parent_input_fingerprint": claim.input_fingerprint,
                        "target_type": claim.target_type,
                        "target_id": claim.target_id,
                        "window": claim.window,
                        "venue": venue,
                    }
                ),
                version=claim.projection_version,
            )

    def release_stale(
        self,
        claim: RadarProjectionClaim,
        *,
        now_ms: int,
    ) -> None:
        with self._session(
            transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
        ) as repos, repos.transaction():
            repos.projection_frontiers.release_stale(
                RADAR_FRONTIER,
                key=claim.key,
                runtime_id=claim.runtime_id,
                now_ms=now_ms,
            )

    def fail_deterministic(
        self,
        claim: RadarProjectionClaim,
        *,
        error_code: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        with self._session(
            transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
        ) as repos, repos.transaction():
            return cast(
                dict[str, Any] | None,
                repos.projection_frontiers.fail_deterministic(
                    RADAR_FRONTIER,
                    key=claim.key,
                    runtime_id=claim.runtime_id,
                    error_code=error_code,
                    now_ms=now_ms,
                ),
            )

    def fail_transient(
        self,
        claim: RadarProjectionClaim,
        *,
        error_code: str,
        now_ms: int,
    ) -> bool:
        with self._session(
            transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
        ) as repos, repos.transaction():
            return bool(
                repos.projection_frontiers.fail_transient(
                    RADAR_FRONTIER,
                    key=claim.key,
                    runtime_id=claim.runtime_id,
                    error_code=error_code,
                    now_ms=now_ms,
                )
            )

    @staticmethod
    def _profile_frontier_callback(repos: Any) -> Any:
        def callback(
            *,
            window: str,
            venue: str,
            rows: list[dict[str, Any]],
            exited_rows: list[dict[str, Any]],
            previous_by_key: dict[tuple[str, str, str], dict[str, Any]],
            computed_at_ms: int,
        ) -> None:
            del window, previous_by_key
            if venue != TOKEN_RADAR_DEFAULT_VENUE:
                return
            targets = {
                (str(row["target_type_key"]), str(row["identity_id"]))
                for row in [*rows, *exited_rows]
                if str(row.get("target_type_key") or "") in {"Asset", "CexToken"}
            }
            if not targets:
                return
            ordered = sorted(targets)
            cursor = repos.conn.execute(
                """
                WITH targets(target_type, target_id) AS (
                  SELECT *
                  FROM unnest(%(target_types)s::text[], %(target_ids)s::text[])
                ),
                serving AS (
                  SELECT
                    target.target_type,
                    target.target_id,
                    count(current.row_id) AS serving_rows,
                    'sha256:' || encode(
                      sha256(
                        convert_to(
                          COALESCE(
                            jsonb_agg(
                              jsonb_build_object(
                                'window', current."window",
                                'venue', current.venue,
                                'lane', current.lane,
                                'payload_hash', current.payload_hash
                              )
                              ORDER BY current."window", current.venue, current.lane
                            ) FILTER (WHERE current.row_id IS NOT NULL),
                            '[]'::jsonb
                          )::text,
                          'UTF8'
                        )
                      ),
                      'hex'
                    ) AS input_fingerprint
                  FROM targets target
                  LEFT JOIN token_radar_current_rows current
                    ON current.projection_version = %(projection_version)s
                   AND current.target_type_key = target.target_type
                   AND current.identity_id = target.target_id
                  GROUP BY target.target_type, target.target_id
                )
                INSERT INTO token_profile_projection_frontiers(
                  target_type, target_id, status, first_dirty_at_ms,
                  deadline_at_ms, next_attempt_at_ms, attempt_count,
                  input_fingerprint, projection_version, claimed_by,
                  claimed_until_ms, last_error_code, updated_at_ms
                )
                SELECT
                  target_type, target_id, 'dirty', %(dirty_at_ms)s,
                  %(deadline_at_ms)s, NULL, 0, input_fingerprint,
                  %(profile_projection_version)s, NULL, NULL, NULL,
                  %(dirty_at_ms)s
                FROM serving
                WHERE serving_rows <= 100
                ON CONFLICT(target_type, target_id) DO UPDATE SET
                  status = CASE
                    WHEN (
                      token_profile_projection_frontiers.input_fingerprint
                        IS DISTINCT FROM EXCLUDED.input_fingerprint
                      OR token_profile_projection_frontiers.projection_version
                        IS DISTINCT FROM EXCLUDED.projection_version
                    ) THEN 'dirty'
                    ELSE token_profile_projection_frontiers.status
                  END,
                  first_dirty_at_ms = CASE
                    WHEN (
                      token_profile_projection_frontiers.input_fingerprint
                        IS DISTINCT FROM EXCLUDED.input_fingerprint
                      OR token_profile_projection_frontiers.projection_version
                        IS DISTINCT FROM EXCLUDED.projection_version
                    )
                      AND token_profile_projection_frontiers.status IN ('clean', 'quarantined')
                    THEN EXCLUDED.first_dirty_at_ms
                    ELSE LEAST(
                      COALESCE(
                        token_profile_projection_frontiers.first_dirty_at_ms,
                        EXCLUDED.first_dirty_at_ms
                      ),
                      EXCLUDED.first_dirty_at_ms
                    )
                  END,
                  deadline_at_ms = CASE
                    WHEN (
                      token_profile_projection_frontiers.input_fingerprint
                        IS DISTINCT FROM EXCLUDED.input_fingerprint
                      OR token_profile_projection_frontiers.projection_version
                        IS DISTINCT FROM EXCLUDED.projection_version
                    )
                      AND token_profile_projection_frontiers.status IN ('clean', 'quarantined')
                    THEN EXCLUDED.deadline_at_ms
                    ELSE LEAST(
                      COALESCE(
                        token_profile_projection_frontiers.deadline_at_ms,
                        EXCLUDED.deadline_at_ms
                      ),
                      EXCLUDED.deadline_at_ms
                    )
                  END,
                  next_attempt_at_ms = CASE
                    WHEN (
                      token_profile_projection_frontiers.input_fingerprint
                        IS DISTINCT FROM EXCLUDED.input_fingerprint
                      OR token_profile_projection_frontiers.projection_version
                        IS DISTINCT FROM EXCLUDED.projection_version
                    ) THEN NULL
                    ELSE token_profile_projection_frontiers.next_attempt_at_ms
                  END,
                  attempt_count = CASE
                    WHEN (
                      token_profile_projection_frontiers.input_fingerprint
                        IS DISTINCT FROM EXCLUDED.input_fingerprint
                      OR token_profile_projection_frontiers.projection_version
                        IS DISTINCT FROM EXCLUDED.projection_version
                    ) THEN 0
                    ELSE token_profile_projection_frontiers.attempt_count
                  END,
                  transient_failure_count = CASE
                    WHEN (
                      token_profile_projection_frontiers.input_fingerprint
                        IS DISTINCT FROM EXCLUDED.input_fingerprint
                      OR token_profile_projection_frontiers.projection_version
                        IS DISTINCT FROM EXCLUDED.projection_version
                    ) THEN 0
                    ELSE token_profile_projection_frontiers.transient_failure_count
                  END,
                  input_fingerprint = EXCLUDED.input_fingerprint,
                  projection_version = EXCLUDED.projection_version,
                  claimed_by = CASE
                    WHEN (
                      token_profile_projection_frontiers.input_fingerprint
                        IS DISTINCT FROM EXCLUDED.input_fingerprint
                      OR token_profile_projection_frontiers.projection_version
                        IS DISTINCT FROM EXCLUDED.projection_version
                    ) THEN NULL
                    ELSE token_profile_projection_frontiers.claimed_by
                  END,
                  claimed_until_ms = CASE
                    WHEN (
                      token_profile_projection_frontiers.input_fingerprint
                        IS DISTINCT FROM EXCLUDED.input_fingerprint
                      OR token_profile_projection_frontiers.projection_version
                        IS DISTINCT FROM EXCLUDED.projection_version
                    ) THEN NULL
                    ELSE token_profile_projection_frontiers.claimed_until_ms
                  END,
                  last_error_code = CASE
                    WHEN (
                      token_profile_projection_frontiers.input_fingerprint
                        IS DISTINCT FROM EXCLUDED.input_fingerprint
                      OR token_profile_projection_frontiers.projection_version
                        IS DISTINCT FROM EXCLUDED.projection_version
                    ) THEN NULL
                    ELSE token_profile_projection_frontiers.last_error_code
                  END,
                  updated_at_ms = EXCLUDED.updated_at_ms
                """,
                {
                    "target_types": [target_type for target_type, _target_id in ordered],
                    "target_ids": [target_id for _target_type, target_id in ordered],
                    "projection_version": TOKEN_RADAR_PROJECTION_VERSION,
                    "dirty_at_ms": int(computed_at_ms),
                    "deadline_at_ms": int(computed_at_ms) + _PROFILE_DEADLINE_MS,
                    "profile_projection_version": "token-profile-current-serving-v1",
                },
            )
            if int(cursor.rowcount or 0) != len(ordered):
                raise RadarShardOversized("radar_profile_serving_input_oversized")

        return callback

    def _session(
        self,
        *,
        transaction_timeout_seconds: float | None = None,
    ) -> Any:
        return self.db.worker_session(
            self.worker_name,
            statement_timeout_seconds=(
                _MAINTENANCE_STATEMENT_TIMEOUT_SECONDS
                if self.worker_name == "radar_maintenance_rebuild"
                else _STEADY_STATEMENT_TIMEOUT_SECONDS
            ),
            transaction_timeout_seconds=transaction_timeout_seconds,
        )


def rebuild_all_token_radar_for_maintenance(
    *,
    db: Any,
    now_ms: int,
) -> dict[str, Any]:
    """Rebuild the current Radar model from recent material facts.

    This full scan is intentionally available only to the maintenance
    composition. Steady workers remain source-edge incremental.
    """

    service = RadarProjectionService(
        db=db,
        worker_name="radar_maintenance_rebuild",
    )
    with service._session() as repos, repos.transaction():
        reset_counts = {
            "current_rows": int(repos.conn.execute("DELETE FROM token_radar_current_rows").rowcount or 0),
            "publication_states": int(repos.conn.execute("DELETE FROM token_radar_publication_state").rowcount or 0),
            "target_features": int(repos.conn.execute("DELETE FROM token_radar_target_features").rowcount or 0),
            "source_edges": int(repos.conn.execute("DELETE FROM radar_source_edges").rowcount or 0),
            "frontiers": int(repos.conn.execute("DELETE FROM radar_projection_frontiers").rowcount or 0),
        }

    event_count = 0
    edge_writes = 0
    after: tuple[int, str] | None = None
    cutoff_ms = int(now_ms) - 48 * 60 * 60 * 1_000
    while True:
        after_predicate = (
            """
              AND (event.received_at_ms, event.event_id)
                  > (%(after_received_at_ms)s, %(after_event_id)s)
            """
            if after is not None
            else ""
        )
        with service._session() as repos:
            rows = repos.conn.execute(
                f"""
                SELECT DISTINCT event.received_at_ms, event.event_id
                FROM events event
                JOIN token_intents intent
                  ON intent.event_id = event.event_id
                JOIN token_intent_resolutions resolution
                  ON resolution.intent_id = intent.intent_id
                 AND resolution.event_id = event.event_id
                WHERE event.received_at_ms >= %(cutoff_ms)s
                  AND resolution.is_current
                  AND resolution.target_type IN ('Asset', 'CexToken')
                  AND resolution.target_id IS NOT NULL
                  {after_predicate}
                ORDER BY event.received_at_ms, event.event_id
                LIMIT %(limit)s
                """,
                {
                    "cutoff_ms": cutoff_ms,
                    "after_received_at_ms": after[0] if after else None,
                    "after_event_id": after[1] if after else None,
                    "limit": _MAINTENANCE_EVENT_BATCH_SIZE,
                },
            ).fetchall()
        if not rows:
            break
        for row in rows:
            with service._session() as repos, repos.transaction():
                edge_writes += repos.radar_source_edges.sync_event(
                    event_id=str(row["event_id"]),
                    now_ms=int(now_ms),
                )
            event_count += 1
        last = rows[-1]
        after = (int(last["received_at_ms"]), str(last["event_id"]))

    runtime_id = str(uuid4())
    shard_results: list[dict[str, Any]] = []
    while True:
        due = service.next_due(now_ms=int(now_ms))
        if due is None:
            break
        if len(shard_results) >= _MAINTENANCE_SHARD_CAP:
            raise RuntimeError("radar_maintenance_shard_cap_exceeded")
        claim = service.claim(
            key={
                "target_type": str(due["target_type"]),
                "target_id": str(due["target_id"]),
                "window_key": str(due["window_key"]),
                "venue": str(due["venue"]),
            },
            runtime_id=runtime_id,
            now_ms=int(now_ms),
        )
        if claim is None:
            raise RuntimeError("radar_maintenance_claim_missing")
        loaded = service.load_target(claim, now_ms=int(now_ms))
        target_projection = compute_token_radar_target_projection(loaded)
        ranked = rank_token_radar_closure(
            {
                **loaded,
                "feature": target_projection["feature"],
                "venues": [claim.venue],
                "rank_limit": _RANK_LIMIT,
            }
        )
        hydrated = service.load_hydration(
            claim,
            target_projection=target_projection,
            ranked=ranked,
        )
        closure = build_token_radar_current_closure(
            {
                "feature": target_projection["feature"],
                "selected_by_venue": ranked["selected_by_venue"],
                "hydrated_inputs": hydrated,
            }
        )
        result = service.publish(
            claim,
            target_projection=target_projection,
            ranked=ranked,
            closure=closure,
            now_ms=int(now_ms),
        )
        if result["projection_status"] not in {"published", "unchanged"}:
            raise RuntimeError(f"radar_maintenance_publish_failed:{result['projection_status']}")
        shard_results.append(result)

    with service._session() as repos:
        quarantined = int(
            repos.conn.execute(
                """
                SELECT count(*) AS count
                FROM radar_projection_frontiers
                WHERE status = 'quarantined'
                """
            ).fetchone()["count"]
        )
        current_rows = int(
            repos.conn.execute("SELECT count(*) AS count FROM token_radar_current_rows").fetchone()["count"]
        )
    if quarantined:
        raise RuntimeError(f"radar_maintenance_quarantine_unresolved:{quarantined}")
    return {
        "projection_status": "rebuilt",
        "events_scanned": event_count,
        "source_edges_written": edge_writes,
        "shards_computed": len(shard_results),
        "rows_written": sum(int(result["rows_written"]) for result in shard_results),
        "current_rows": current_rows,
        "reset": reset_counts,
    }


def _claim_still_current(
    row: Any,
    claim: RadarProjectionClaim,
) -> bool:
    if row is None:
        return False
    return (
        str(row["status"]) == "running"
        and str(row["claimed_by"]) == claim.runtime_id
        and str(row["input_fingerprint"]) == claim.input_fingerprint
        and str(row["projection_version"]) == claim.projection_version
    )


def _fingerprint(value: Any) -> str:
    serialized = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def _serialized_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    )


def _require_bounded_input(payload: dict[str, Any]) -> None:
    row_count = len(payload.get("source_rows", [])) + len(payload.get("compact_inputs", []))
    if row_count > _INPUT_ROW_CAP:
        raise RadarShardOversized("radar_input_row_overflow")
    if _serialized_size(payload) > _INPUT_BYTE_CAP:
        raise RadarShardOversized("radar_input_byte_overflow")


def _require_bounded_output(payload: dict[str, Any]) -> None:
    if _serialized_size(payload) > _OUTPUT_BYTE_CAP:
        raise RadarShardOversized("radar_output_byte_overflow")


__all__ = [
    "RadarProjectionClaim",
    "RadarProjectionService",
    "RadarShardOversized",
    "build_token_radar_current_closure",
    "compute_token_radar_target_projection",
    "rank_token_radar_closure",
    "rebuild_all_token_radar_for_maintenance",
]
