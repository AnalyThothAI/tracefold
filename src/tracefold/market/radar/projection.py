from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID, uuid4

from tracefold.market.radar.constants import (
    TOKEN_RADAR_DEFAULT_VENUE,
    TOKEN_RADAR_PROJECTION_VERSION,
    TOKEN_RADAR_RESOLVER_POLICY_VERSION,
    TOKEN_RADAR_VENUES,
    WINDOW_MS,
)
from tracefold.market.radar.output_envelope import (
    OutputRowOversized,
    split_bounded_rows,
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
_STOCK_SOURCE_EVENT_LIMIT = 25
_MAINTENANCE_EVENT_BATCH_SIZE = 1_000
_MAINTENANCE_SHARD_CAP = 1_000_000
_RANK_SET_TARGET_TYPE = "RankSet"
_TOKEN_RANK_SET_TARGET_ID = "token"
_STOCK_RANK_SET_TARGET_ID = "stocks"
_TARGET_FEATURE_LEAD_MS = {
    "5m": 5_000,
    "1h": 30_000,
    "4h": 30_000,
    "24h": 30_000,
}


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
    first_dirty_at_ms: int
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
        with (
            self._session(
                transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            repos.radar_source_edges.expire_due(
                now_ms=now_ms,
                limit=_EXPIRY_BATCH_SIZE,
            )
            row = repos.conn.execute(
                """
                SELECT
                  frontier.*,
                  frontier.deadline_at_ms
                    - CASE
                        WHEN frontier.target_type = %(rank_set_type)s
                          THEN 0
                        WHEN frontier.window_key = '5m'
                          THEN %(lead_5m)s
                        ELSE %(lead_other)s
                      END AS effective_deadline_at_ms
                FROM radar_projection_frontiers frontier
                WHERE frontier.deadline_at_ms IS NOT NULL
                  AND (
                    frontier.status = 'dirty'
                    OR (
                      frontier.status = 'retry_wait'
                      AND COALESCE(
                        frontier.next_attempt_at_ms,
                        frontier.deadline_at_ms
                      ) <= %(now_ms)s
                    )
                    OR (
                      frontier.status = 'running'
                      AND frontier.claimed_until_ms <= %(now_ms)s
                    )
                  )
                ORDER BY
                  effective_deadline_at_ms,
                  frontier.window_key,
                  frontier.venue,
                  frontier.target_type,
                  frontier.target_id
                LIMIT 1
                """,
                {
                    "rank_set_type": _RANK_SET_TARGET_TYPE,
                    "lead_5m": _TARGET_FEATURE_LEAD_MS["5m"],
                    "lead_other": _TARGET_FEATURE_LEAD_MS["1h"],
                    "now_ms": int(now_ms),
                },
            ).fetchone()
            return dict(row) if row is not None else None

    def claim(
        self,
        *,
        key: dict[str, str],
        runtime_id: str,
        now_ms: int,
    ) -> RadarProjectionClaim | None:
        with (
            self._session(
                transaction_timeout_seconds=_CLAIM_TRANSACTION_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
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
            first_dirty_at_ms=int(row["first_dirty_at_ms"]),
            deadline_at_ms=int(row["deadline_at_ms"]),
        )

    def load_target_feature(
        self,
        claim: RadarProjectionClaim,
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        if claim.target_type == _RANK_SET_TARGET_TYPE:
            raise ValueError("radar_rank_set_is_not_target_feature")
        if claim.target_type == "MarketInstrument":
            return self._load_stock_target_feature(claim, now_ms=now_ms)
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
            "old_venues": sorted(venues),
        }
        _require_bounded_input(loaded)
        return loaded

    def load_rank_set(
        self,
        claim: RadarProjectionClaim,
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        if claim.target_type != _RANK_SET_TARGET_TYPE:
            raise ValueError("radar_target_feature_is_not_rank_set")
        if claim.target_id == _STOCK_RANK_SET_TARGET_ID:
            return self._load_stock_rank_set(claim, now_ms=now_ms)
        if claim.target_id != _TOKEN_RANK_SET_TARGET_ID:
            raise ValueError("radar_rank_set_target_invalid")
        window_ms = WINDOW_MS[claim.window]
        with self._session() as repos:
            compact_inputs = repos.token_radar.list_compact_rank_inputs_for_rank_set(
                projection_version=TOKEN_RADAR_PROJECTION_VERSION,
                window=claim.window,
                min_latest_event_received_at_ms=int(now_ms) - window_ms,
                row_cap=_INPUT_ROW_CAP,
            )
        loaded = {
            "target_type": claim.target_type,
            "target_id": claim.target_id,
            "window": claim.window,
            "now_ms": int(now_ms),
            "compact_inputs": compact_inputs,
        }
        _require_bounded_input(loaded)
        return loaded

    def _load_stock_target_feature(
        self,
        claim: RadarProjectionClaim,
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        with self._session() as repos:
            rows = [
                dict(row)
                for row in repos.conn.execute(
                    """
                    SELECT edge.source_id AS event_id,
                           edge.observed_at_ms AS received_at_ms,
                           event.author_handle,
                           COALESCE(event.text_clean, event.text) AS text,
                           equity.symbol,
                           equity.security_name,
                           equity.exchange,
                           equity.instrument_type
                    FROM radar_source_edges edge
                    JOIN events event
                      ON event.event_id = edge.source_id
                    JOIN us_equity_symbols equity
                      ON equity.market_instrument_id = edge.target_id
                     AND equity.status = 'active'
                    WHERE edge.target_type = 'MarketInstrument'
                      AND edge.target_id = %s
                      AND edge.window_key = %s
                      AND edge.venue = %s
                      AND edge.source_kind = 'event'
                      AND edge.observed_at_ms >= %s
                    ORDER BY edge.observed_at_ms DESC, edge.source_id DESC
                    LIMIT %s
                    """,
                    (
                        claim.target_id,
                        claim.window,
                        claim.venue,
                        int(now_ms) - WINDOW_MS[claim.window],
                        _INPUT_ROW_CAP + 1,
                    ),
                ).fetchall()
            ]
            if len(rows) > _INPUT_ROW_CAP:
                raise RadarShardOversized("stocks_radar_target_shard_oversized")
        loaded = {
            "kind": "stocks_radar_target_feature",
            "target_id": claim.target_id,
            "window": claim.window,
            "now_ms": int(now_ms),
            "rows": rows,
        }
        _require_bounded_input(loaded)
        return loaded

    def _load_stock_rank_set(
        self,
        claim: RadarProjectionClaim,
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        with self._session() as repos:
            current_features = [
                dict(row)
                for row in repos.conn.execute(
                    """
                    SELECT *
                    FROM stock_attention_target_features
                    WHERE window_key = %s
                    ORDER BY target_id
                    LIMIT %s
                    """,
                    (claim.window, _INPUT_ROW_CAP + 1),
                ).fetchall()
            ]
            if len(current_features) > _INPUT_ROW_CAP:
                raise RadarShardOversized("stocks_radar_rank_set_oversized")
        loaded = {
            "kind": "stocks_radar_rank_set",
            "target_id": claim.target_id,
            "window": claim.window,
            "now_ms": int(now_ms),
            "current_features": current_features,
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

    def publish_target_feature(
        self,
        claim: RadarProjectionClaim,
        *,
        target_projection: dict[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        if claim.target_type in {_RANK_SET_TARGET_TYPE, "MarketInstrument"}:
            raise ValueError("radar_token_target_feature_claim_invalid")
        with (
            self._session(
                transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            frontier = self._locked_frontier(repos, claim)
            if not _claim_still_current(frontier, claim):
                return {
                    "projection_status": "stale_snapshot",
                    "rows_written": 0,
                }
            feature_writes = self._write_token_target_feature(
                repos,
                claim=claim,
                target_projection=target_projection,
                now_ms=now_ms,
            )
            rank_frontiers = 0
            if feature_writes:
                venues = {
                    TOKEN_RADAR_DEFAULT_VENUE,
                    *(str(item) for item in target_projection["old_venues"]),
                    str(target_projection["target_venue"]),
                }
                for venue in sorted(venues & set(TOKEN_RADAR_VENUES)):
                    rank_frontiers += self._mark_rank_set_dirty(
                        repos,
                        target_id=_TOKEN_RANK_SET_TARGET_ID,
                        window=claim.window,
                        venue=venue,
                        dirty_at_ms=claim.first_dirty_at_ms,
                        deadline_at_ms=claim.deadline_at_ms,
                        input_fingerprint=_fingerprint(
                            {
                                "kind": "token_rank_set",
                                "parent_input_fingerprint": claim.input_fingerprint,
                                "target_type": claim.target_type,
                                "target_id": claim.target_id,
                                "window": claim.window,
                                "venue": venue,
                                "feature": target_projection.get("feature"),
                            }
                        ),
                        now_ms=now_ms,
                    )
            if not repos.projection_frontiers.complete(
                RADAR_FRONTIER,
                key=claim.key,
                runtime_id=claim.runtime_id,
                input_fingerprint=claim.input_fingerprint,
                version=claim.projection_version,
                now_ms=int(now_ms),
            ):
                raise RuntimeError("radar_target_feature_completion_cas_mismatch")
        return {
            "projection_status": ("published" if feature_writes else "unchanged"),
            "rows_written": feature_writes,
            "feature_rows_written": feature_writes,
            "rank_frontiers_written": rank_frontiers,
            "source_rows": int(target_projection["source_rows"]),
            "target_type": claim.target_type,
            "target_id": claim.target_id,
            "window": claim.window,
        }

    def publish_token_rank_set(
        self,
        claim: RadarProjectionClaim,
        *,
        ranked: dict[str, Any],
        closure: dict[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        if claim.target_type != _RANK_SET_TARGET_TYPE or claim.target_id != _TOKEN_RANK_SET_TARGET_ID:
            raise ValueError("radar_token_rank_set_claim_invalid")
        _require_bounded_output(closure)
        if set(closure["rows_by_venue"]) != {claim.venue}:
            raise RuntimeError("radar_publish_cross_venue_shard_forbidden")
        with (
            self._session(
                transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            frontier = self._locked_frontier(repos, claim)
            if not _claim_still_current(frontier, claim):
                return {
                    "projection_status": "stale_snapshot",
                    "rows_written": 0,
                }
            rows = [dict(row) for row in closure["rows_by_venue"][claim.venue]]
            generation_id = stable_generation_id(
                projection_version=TOKEN_RADAR_PROJECTION_VERSION,
                window=claim.window,
                venue=claim.venue,
                rows=rows,
            )
            source_frontier_ms = max(
                (int(row.get("source_max_received_at_ms") or 0) for row in rows),
                default=0,
            )
            result = repos.token_radar.publish_current_generation(
                projection_version=TOKEN_RADAR_PROJECTION_VERSION,
                window=claim.window,
                venue=claim.venue,
                generation_id=generation_id,
                published_at_ms=int(now_ms),
                source_frontier_ms=source_frontier_ms,
                rows=rows,
                source_rows=int(ranked["source_rows_by_venue"].get(claim.venue, 0)),
                started_at_ms=int(now_ms),
                finished_at_ms=int(now_ms),
                on_current_changes=self._profile_frontier_callback(repos),
            )
            status = str(result["status"])
            if status not in {"published", "unchanged"}:
                raise RuntimeError(f"radar_atomic_publication_failed:{claim.venue}:{status}")
            next_status = self._complete_rank_frontier(
                repos,
                claim=claim,
                now_ms=now_ms,
            )
        return {
            "projection_status": status,
            "rows_written": int(result["rows_written"]),
            "source_rows": int(ranked["source_rows_by_venue"].get(claim.venue, 0)),
            "ranked_rows": len(rows),
            "rank_set": claim.target_id,
            "window": claim.window,
            "venue": claim.venue,
            "frontier_status": next_status,
        }

    @staticmethod
    def _locked_frontier(
        repos: Any,
        claim: RadarProjectionClaim,
    ) -> Any:
        return repos.conn.execute(
            """
            SELECT status, claimed_by, input_fingerprint,
                   projection_version
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

    @staticmethod
    def _write_token_target_feature(
        repos: Any,
        *,
        claim: RadarProjectionClaim,
        target_projection: dict[str, Any],
        now_ms: int,
    ) -> int:
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
        return feature_writes

    @staticmethod
    def _mark_rank_set_dirty(
        repos: Any,
        *,
        target_id: str,
        window: str,
        venue: str,
        dirty_at_ms: int,
        deadline_at_ms: int,
        input_fingerprint: str,
        now_ms: int,
    ) -> int:
        cursor = repos.conn.execute(
            """
            INSERT INTO radar_projection_frontiers(
              target_type, target_id, window_key, venue, status,
              first_dirty_at_ms, deadline_at_ms, next_attempt_at_ms,
              attempt_count, transient_failure_count, input_fingerprint,
              projection_version, claimed_by, claimed_until_ms,
              last_error_code, updated_at_ms
            )
            VALUES (
              %(target_type)s, %(target_id)s, %(window)s, %(venue)s,
              'dirty', %(dirty_at_ms)s, %(deadline_at_ms)s, NULL,
              0, 0, %(input_fingerprint)s, %(projection_version)s,
              NULL, NULL, NULL, %(now_ms)s
            )
            ON CONFLICT(target_type, target_id, window_key, venue)
            DO UPDATE SET
              status = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN 'running'
                ELSE 'dirty'
              END,
              first_dirty_at_ms = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN radar_projection_frontiers.first_dirty_at_ms
                ELSE LEAST(
                  COALESCE(
                    radar_projection_frontiers.first_dirty_at_ms,
                    EXCLUDED.first_dirty_at_ms
                  ),
                  EXCLUDED.first_dirty_at_ms
                )
              END,
              deadline_at_ms = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN radar_projection_frontiers.deadline_at_ms
                ELSE LEAST(
                  COALESCE(
                    radar_projection_frontiers.deadline_at_ms,
                    EXCLUDED.deadline_at_ms
                  ),
                  EXCLUDED.deadline_at_ms
                )
              END,
              pending_first_dirty_at_ms = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN LEAST(
                    COALESCE(
                      radar_projection_frontiers.pending_first_dirty_at_ms,
                      EXCLUDED.first_dirty_at_ms
                    ),
                    EXCLUDED.first_dirty_at_ms
                  )
                ELSE NULL
              END,
              pending_deadline_at_ms = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN LEAST(
                    COALESCE(
                      radar_projection_frontiers.pending_deadline_at_ms,
                      EXCLUDED.deadline_at_ms
                    ),
                    EXCLUDED.deadline_at_ms
                  )
                ELSE NULL
              END,
              pending_input_fingerprint = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN EXCLUDED.input_fingerprint
                ELSE NULL
              END,
              pending_projection_version = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN EXCLUDED.projection_version
                ELSE NULL
              END,
              next_attempt_at_ms = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN radar_projection_frontiers.next_attempt_at_ms
                ELSE NULL
              END,
              attempt_count = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN radar_projection_frontiers.attempt_count
                ELSE 0
              END,
              transient_failure_count = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN radar_projection_frontiers.transient_failure_count
                ELSE 0
              END,
              input_fingerprint = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN radar_projection_frontiers.input_fingerprint
                ELSE EXCLUDED.input_fingerprint
              END,
              projection_version = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN radar_projection_frontiers.projection_version
                ELSE EXCLUDED.projection_version
              END,
              claimed_by = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN radar_projection_frontiers.claimed_by
                ELSE NULL
              END,
              claimed_until_ms = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN radar_projection_frontiers.claimed_until_ms
                ELSE NULL
              END,
              last_error_code = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN radar_projection_frontiers.last_error_code
                ELSE NULL
              END,
              updated_at_ms = EXCLUDED.updated_at_ms
            """,
            {
                "target_type": _RANK_SET_TARGET_TYPE,
                "target_id": target_id,
                "window": window,
                "venue": venue,
                "dirty_at_ms": int(dirty_at_ms),
                "deadline_at_ms": int(deadline_at_ms),
                "input_fingerprint": input_fingerprint,
                "projection_version": TOKEN_RADAR_PROJECTION_VERSION,
                "now_ms": int(now_ms),
            },
        )
        return int(cursor.rowcount or 0)

    @staticmethod
    def _complete_rank_frontier(
        repos: Any,
        *,
        claim: RadarProjectionClaim,
        now_ms: int,
    ) -> str:
        row = repos.conn.execute(
            """
            UPDATE radar_projection_frontiers
            SET status = CASE
                  WHEN pending_input_fingerprint IS NULL
                    THEN 'clean'
                  ELSE 'dirty'
                END,
                first_dirty_at_ms = pending_first_dirty_at_ms,
                deadline_at_ms = pending_deadline_at_ms,
                next_attempt_at_ms = NULL,
                attempt_count = 0,
                transient_failure_count = 0,
                input_fingerprint = COALESCE(
                  pending_input_fingerprint,
                  input_fingerprint
                ),
                projection_version = COALESCE(
                  pending_projection_version,
                  projection_version
                ),
                claimed_by = NULL,
                claimed_until_ms = NULL,
                last_error_code = NULL,
                pending_first_dirty_at_ms = NULL,
                pending_deadline_at_ms = NULL,
                pending_input_fingerprint = NULL,
                pending_projection_version = NULL,
                updated_at_ms = %(now_ms)s
            WHERE target_type = %(target_type)s
              AND target_id = %(target_id)s
              AND window_key = %(window)s
              AND venue = %(venue)s
              AND status = 'running'
              AND claimed_by = %(runtime_id)s
              AND input_fingerprint = %(input_fingerprint)s
              AND projection_version = %(projection_version)s
            RETURNING status
            """,
            {
                **claim.key,
                "window": claim.window,
                "runtime_id": UUID(claim.runtime_id),
                "input_fingerprint": claim.input_fingerprint,
                "projection_version": claim.projection_version,
                "now_ms": int(now_ms),
            },
        ).fetchone()
        if row is None:
            raise RuntimeError("radar_rank_frontier_completion_cas_mismatch")
        return str(row["status"])

    def publish_stock_target_feature(
        self,
        claim: RadarProjectionClaim,
        *,
        target_projection: dict[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        if claim.target_type != "MarketInstrument":
            raise ValueError("stocks_radar_target_feature_claim_invalid")
        with (
            self._session(
                transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            frontier = self._locked_frontier(repos, claim)
            if not _claim_still_current(frontier, claim):
                return {
                    "projection_status": "stale_snapshot",
                    "rows_written": 0,
                }
            feature_writes = self._write_stock_target_feature(
                repos,
                claim=claim,
                feature=target_projection.get("feature"),
            )
            rank_frontiers = 0
            if feature_writes:
                rank_frontiers = self._mark_rank_set_dirty(
                    repos,
                    target_id=_STOCK_RANK_SET_TARGET_ID,
                    window=claim.window,
                    venue=TOKEN_RADAR_DEFAULT_VENUE,
                    dirty_at_ms=claim.first_dirty_at_ms,
                    deadline_at_ms=claim.deadline_at_ms,
                    input_fingerprint=_fingerprint(
                        {
                            "kind": "stocks_rank_set",
                            "parent_input_fingerprint": claim.input_fingerprint,
                            "target_id": claim.target_id,
                            "window": claim.window,
                            "feature": target_projection.get("feature"),
                        }
                    ),
                    now_ms=now_ms,
                )
            if not repos.projection_frontiers.complete(
                RADAR_FRONTIER,
                key=claim.key,
                runtime_id=claim.runtime_id,
                input_fingerprint=claim.input_fingerprint,
                version=claim.projection_version,
                now_ms=now_ms,
            ):
                raise RuntimeError("stocks_radar_target_feature_completion_cas_failed")
        return {
            "projection_status": ("published" if feature_writes else "unchanged"),
            "rows_written": feature_writes,
            "feature_rows_written": feature_writes,
            "rank_frontiers_written": rank_frontiers,
            "source_rows": int(target_projection["source_rows"]),
            "target_id": claim.target_id,
            "window": claim.window,
        }

    def publish_stock_rank_set(
        self,
        claim: RadarProjectionClaim,
        *,
        projection: dict[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        if claim.target_type != _RANK_SET_TARGET_TYPE or claim.target_id != _STOCK_RANK_SET_TARGET_ID:
            raise ValueError("stocks_radar_rank_set_claim_invalid")
        _require_bounded_output(projection)
        with (
            self._session(
                transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            frontier = self._locked_frontier(repos, claim)
            if not _claim_still_current(frontier, claim):
                return {
                    "projection_status": "stale_snapshot",
                    "rows_written": 0,
                }
            publication_writes = self._publish_stock_rank_rows(
                repos,
                claim=claim,
                projection=projection,
                now_ms=now_ms,
            )
            next_status = self._complete_rank_frontier(
                repos,
                claim=claim,
                now_ms=now_ms,
            )
        return {
            "projection_status": ("published" if publication_writes else "unchanged"),
            "rows_written": publication_writes,
            "publication_rows_written": publication_writes,
            "ranked_rows": len(projection["rows"]),
            "rank_set": claim.target_id,
            "window": claim.window,
            "venue": claim.venue,
            "frontier_status": next_status,
        }

    @staticmethod
    def _write_stock_target_feature(
        repos: Any,
        *,
        claim: RadarProjectionClaim,
        feature: Any,
    ) -> int:
        if not isinstance(feature, dict):
            return int(
                repos.conn.execute(
                    """
                    DELETE FROM stock_attention_target_features
                    WHERE window_key = %s
                      AND target_id = %s
                    """,
                    (claim.window, claim.target_id),
                ).rowcount
                or 0
            )
        return int(
            repos.conn.execute(
                """
                INSERT INTO stock_attention_target_features (
                  window_key, target_id, symbol, security_name,
                  exchange, instrument_type, mentions, unique_authors,
                  latest_seen_ms, latest_event_id,
                  latest_author_handle, latest_text, source_event_ids,
                  state_fingerprint, computed_at_ms
                )
                VALUES (
                  %(window_key)s, %(target_id)s, %(symbol)s,
                  %(security_name)s, %(exchange)s,
                  %(instrument_type)s, %(mentions)s,
                  %(unique_authors)s, %(latest_seen_ms)s,
                  %(latest_event_id)s, %(latest_author_handle)s,
                  %(latest_text)s, %(source_event_ids)s,
                  %(state_fingerprint)s, %(computed_at_ms)s
                )
                ON CONFLICT (window_key, target_id) DO UPDATE SET
                  symbol = EXCLUDED.symbol,
                  security_name = EXCLUDED.security_name,
                  exchange = EXCLUDED.exchange,
                  instrument_type = EXCLUDED.instrument_type,
                  mentions = EXCLUDED.mentions,
                  unique_authors = EXCLUDED.unique_authors,
                  latest_seen_ms = EXCLUDED.latest_seen_ms,
                  latest_event_id = EXCLUDED.latest_event_id,
                  latest_author_handle = EXCLUDED.latest_author_handle,
                  latest_text = EXCLUDED.latest_text,
                  source_event_ids = EXCLUDED.source_event_ids,
                  state_fingerprint = EXCLUDED.state_fingerprint,
                  computed_at_ms = EXCLUDED.computed_at_ms
                WHERE stock_attention_target_features.state_fingerprint
                      IS DISTINCT FROM EXCLUDED.state_fingerprint
                """,
                feature,
            ).rowcount
            or 0
        )

    @staticmethod
    def _publish_stock_rank_rows(
        repos: Any,
        *,
        claim: RadarProjectionClaim,
        projection: dict[str, Any],
        now_ms: int,
    ) -> int:
        state = repos.conn.execute(
            """
            SELECT state_fingerprint
            FROM stocks_radar_publication_state
            WHERE window_key = %s
            FOR UPDATE
            """,
            (claim.window,),
        ).fetchone()
        if state is not None and str(state["state_fingerprint"]) == str(projection["state_fingerprint"]):
            return 0
        publication_writes = int(
            repos.conn.execute(
                """
                DELETE FROM stocks_radar_current_rows
                WHERE window_key = %s
                """,
                (claim.window,),
            ).rowcount
            or 0
        )
        for row in projection["rows"]:
            publication_writes += int(
                repos.conn.execute(
                    """
                    INSERT INTO stocks_radar_current_rows (
                      window_key, target_id, rank, symbol,
                      security_name, exchange, instrument_type,
                      mentions, unique_authors, latest_seen_ms,
                      latest_event_id, latest_author_handle,
                      latest_text, source_event_ids,
                      state_fingerprint, computed_at_ms
                    )
                    VALUES (
                      %(window_key)s, %(target_id)s, %(rank)s,
                      %(symbol)s, %(security_name)s, %(exchange)s,
                      %(instrument_type)s, %(mentions)s,
                      %(unique_authors)s, %(latest_seen_ms)s,
                      %(latest_event_id)s, %(latest_author_handle)s,
                      %(latest_text)s, %(source_event_ids)s,
                      %(state_fingerprint)s, %(computed_at_ms)s
                    )
                    """,
                    row,
                ).rowcount
                or 0
            )
        publication_writes += int(
            repos.conn.execute(
                """
                INSERT INTO stocks_radar_publication_state (
                  window_key, state_fingerprint,
                  source_frontier_ms, published_at_ms
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (window_key) DO UPDATE SET
                  state_fingerprint = EXCLUDED.state_fingerprint,
                  source_frontier_ms = EXCLUDED.source_frontier_ms,
                  published_at_ms = EXCLUDED.published_at_ms
                """,
                (
                    claim.window,
                    projection["state_fingerprint"],
                    projection["source_frontier_ms"],
                    int(now_ms),
                ),
            ).rowcount
            or 0
        )
        return publication_writes

    def release_stale(
        self,
        claim: RadarProjectionClaim,
        *,
        now_ms: int,
    ) -> None:
        with (
            self._session(
                transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
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
        with (
            self._session(
                transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            if claim.target_type == _RANK_SET_TARGET_TYPE:
                pending = self._promote_pending_rank_frontier(
                    repos,
                    claim=claim,
                    now_ms=now_ms,
                )
                if pending is not None:
                    return pending
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
        with (
            self._session(
                transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            if claim.target_type == _RANK_SET_TARGET_TYPE:
                pending = self._promote_pending_rank_frontier(
                    repos,
                    claim=claim,
                    now_ms=now_ms,
                )
                if pending is not None:
                    return True
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
    def _promote_pending_rank_frontier(
        repos: Any,
        *,
        claim: RadarProjectionClaim,
        now_ms: int,
    ) -> dict[str, Any] | None:
        row = repos.conn.execute(
            """
            UPDATE radar_projection_frontiers
            SET status = 'dirty',
                first_dirty_at_ms = pending_first_dirty_at_ms,
                deadline_at_ms = pending_deadline_at_ms,
                next_attempt_at_ms = NULL,
                attempt_count = 0,
                transient_failure_count = 0,
                input_fingerprint = pending_input_fingerprint,
                projection_version = pending_projection_version,
                claimed_by = NULL,
                claimed_until_ms = NULL,
                last_error_code = NULL,
                pending_first_dirty_at_ms = NULL,
                pending_deadline_at_ms = NULL,
                pending_input_fingerprint = NULL,
                pending_projection_version = NULL,
                updated_at_ms = %(now_ms)s
            WHERE target_type = %(target_type)s
              AND target_id = %(target_id)s
              AND window_key = %(window_key)s
              AND venue = %(venue)s
              AND status = 'running'
              AND claimed_by = %(runtime_id)s
              AND input_fingerprint = %(input_fingerprint)s
              AND projection_version = %(projection_version)s
              AND pending_input_fingerprint IS NOT NULL
            RETURNING *
            """,
            {
                **claim.key,
                "runtime_id": UUID(claim.runtime_id),
                "input_fingerprint": claim.input_fingerprint,
                "projection_version": claim.projection_version,
                "now_ms": int(now_ms),
            },
        ).fetchone()
        return dict(row) if row is not None else None

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


def compute_stocks_radar_target_feature(
    payload: dict[str, Any],
) -> dict[str, Any]:
    target_id = str(payload["target_id"])
    window = str(payload["window"])
    now_ms = int(payload["now_ms"])
    rows_by_event = {str(row["event_id"]): dict(row) for row in payload.get("rows", [])}
    ordered_events = sorted(
        rows_by_event.values(),
        key=lambda row: (
            -int(row["received_at_ms"]),
            str(row["event_id"]),
        ),
    )
    feature: dict[str, Any] | None = None
    if ordered_events:
        latest = ordered_events[0]
        author_handles = {
            str(row.get("author_handle") or "").strip().lower()
            for row in ordered_events
            if str(row.get("author_handle") or "").strip()
        }
        feature_state = {
            "window_key": window,
            "target_id": target_id,
            "symbol": str(latest["symbol"]),
            "security_name": str(latest["security_name"]),
            "exchange": str(latest["exchange"]),
            "instrument_type": str(latest["instrument_type"]),
            "mentions": len(ordered_events),
            "unique_authors": len(author_handles),
            "latest_seen_ms": int(latest["received_at_ms"]),
            "latest_event_id": str(latest["event_id"]),
            "latest_author_handle": (str(latest["author_handle"]) if latest.get("author_handle") else None),
            "latest_text": str(latest["text"]),
            "source_event_ids": [str(row["event_id"]) for row in ordered_events[:_STOCK_SOURCE_EVENT_LIMIT]],
        }
        feature = {
            **feature_state,
            "state_fingerprint": _fingerprint(feature_state),
            "computed_at_ms": now_ms,
        }
    return {
        "feature": feature,
        "source_rows": len(ordered_events),
        "target_id": target_id,
        "window": window,
    }


def compute_stocks_radar_rank_set(
    payload: dict[str, Any],
) -> dict[str, Any]:
    window = str(payload["window"])
    now_ms = int(payload["now_ms"])
    features = {str(row["target_id"]): dict(row) for row in payload.get("current_features", [])}
    ranked_features = sorted(
        features.values(),
        key=lambda row: (
            -int(row["mentions"]),
            -int(row["latest_seen_ms"]),
            str(row["symbol"]),
            str(row["target_id"]),
        ),
    )[:_RANK_LIMIT]
    ranked_rows = [
        {
            **{key: value for key, value in row.items() if key != "rank"},
            "rank": rank,
            "window_key": window,
            "computed_at_ms": now_ms,
        }
        for rank, row in enumerate(ranked_features, start=1)
    ]
    state_fingerprint = _fingerprint(
        [
            [
                row["rank"],
                row["target_id"],
                row["state_fingerprint"],
            ]
            for row in ranked_rows
        ]
    )
    return {
        "rows": ranked_rows,
        "state_fingerprint": state_fingerprint,
        "source_frontier_ms": max(
            (int(row["latest_seen_ms"]) for row in ranked_rows),
            default=0,
        ),
    }


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
            "stocks_current_rows": int(repos.conn.execute("DELETE FROM stocks_radar_current_rows").rowcount or 0),
            "stocks_publication_states": int(
                repos.conn.execute("DELETE FROM stocks_radar_publication_state").rowcount or 0
            ),
            "stocks_target_features": int(
                repos.conn.execute("DELETE FROM stock_attention_target_features").rowcount or 0
            ),
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
                  AND (
                    resolution.target_type IN ('Asset', 'CexToken')
                    OR (
                      resolution.target_type = 'MarketInstrument'
                      AND resolution.resolution_status = 'NON_CRYPTO'
                      AND resolution.resolver_policy_version =
                            %(resolver_policy_version)s
                      AND resolution.reason_codes_json
                            @> '["CONFIRMED_US_EQUITY"]'::jsonb
                    )
                  )
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
                    "resolver_policy_version": TOKEN_RADAR_RESOLVER_POLICY_VERSION,
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
        if claim.target_type == _RANK_SET_TARGET_TYPE:
            loaded = service.load_rank_set(claim, now_ms=int(now_ms))
            if claim.target_id == _STOCK_RANK_SET_TARGET_ID:
                result = service.publish_stock_rank_set(
                    claim,
                    projection=compute_stocks_radar_rank_set(loaded),
                    now_ms=int(now_ms),
                )
            else:
                ranked = rank_token_radar_closure(
                    {
                        **loaded,
                        "feature": None,
                        "venues": [claim.venue],
                        "rank_limit": _RANK_LIMIT,
                    }
                )
                hydrated = service.load_hydration(
                    claim,
                    target_projection={},
                    ranked=ranked,
                )
                closure = build_token_radar_current_closure(
                    {
                        "feature": None,
                        "selected_by_venue": ranked["selected_by_venue"],
                        "hydrated_inputs": hydrated,
                    }
                )
                result = service.publish_token_rank_set(
                    claim,
                    ranked=ranked,
                    closure=closure,
                    now_ms=int(now_ms),
                )
        else:
            loaded = service.load_target_feature(
                claim,
                now_ms=int(now_ms),
            )
            if claim.target_type == "MarketInstrument":
                result = service.publish_stock_target_feature(
                    claim,
                    target_projection=compute_stocks_radar_target_feature(loaded),
                    now_ms=int(now_ms),
                )
            else:
                result = service.publish_target_feature(
                    claim,
                    target_projection=compute_token_radar_target_projection(loaded),
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
        stocks_current_rows = int(
            repos.conn.execute("SELECT count(*) AS count FROM stocks_radar_current_rows").fetchone()["count"]
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
        "stocks_current_rows": stocks_current_rows,
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
    row_count = (
        len(payload.get("source_rows", []))
        + len(payload.get("compact_inputs", []))
        + len(payload.get("rows", []))
        + len(payload.get("current_features", []))
    )
    if row_count > _INPUT_ROW_CAP:
        raise RadarShardOversized("radar_input_row_overflow")
    if _serialized_size(payload) > _INPUT_BYTE_CAP:
        raise RadarShardOversized("radar_input_byte_overflow")


def _require_bounded_output(payload: dict[str, Any]) -> None:
    _require_bounded_output_rows(
        payload.get("rows", []),
        context={"output_kind": "rows"},
    )
    for venue, rows in dict(payload.get("rows_by_venue") or {}).items():
        rows_by_lane: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            lane = str(row.get("lane") or "")
            rows_by_lane.setdefault(lane, []).append(dict(row))
        for lane, lane_rows in rows_by_lane.items():
            _require_bounded_output_rows(
                lane_rows,
                context={
                    "window_venue_lane": [str(venue), lane],
                },
            )


def _require_bounded_output_rows(
    rows: Any,
    *,
    context: dict[str, Any],
) -> None:
    try:
        split_bounded_rows(
            [dict(row) for row in rows],
            context=context,
            byte_cap=_OUTPUT_BYTE_CAP,
        )
    except OutputRowOversized as exc:
        raise RadarShardOversized("radar_output_byte_overflow") from exc


__all__ = [
    "RadarProjectionClaim",
    "RadarProjectionService",
    "RadarShardOversized",
    "build_token_radar_current_closure",
    "compute_stocks_radar_rank_set",
    "compute_stocks_radar_target_feature",
    "compute_token_radar_target_projection",
    "rank_token_radar_closure",
    "rebuild_all_token_radar_for_maintenance",
]
